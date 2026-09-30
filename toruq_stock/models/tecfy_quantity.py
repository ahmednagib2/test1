import logging
import math
from datetime import timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

FULL_BATCH = 5000
POST_BATCH = 100
MAX_FAILS = 5


class TecfySallaQuantity(models.Model):
    _inherit = 'tecfysalla.quantity'

    toruq_real_qty = fields.Integer(string='Odoo real qty', readonly=True)
    toruq_reserve = fields.Integer(string='Showroom reserve', readonly=True)
    toruq_fail_count = fields.Integer(string='Failed posts', default=0)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @api.model
    def _toruq_qty_map(self, product_ids, location_ids):
        if not product_ids or not location_ids:
            return {}
        rows = self.env['stock.quant'].sudo()._read_group(
            [('product_id', 'in', product_ids), ('location_id', 'in', location_ids)],
            ['product_id'], ['available_quantity:sum'])
        return {product.id: qty for product, qty in rows}

    @api.model
    def _toruq_calc(self, product, company, real_qty):
        """Return (real, reserve, qty_for_salla)."""
        real = int(math.floor(real_qty or 0.0))
        reserve = 0
        if company.toruq_safety_enabled:
            tmpl = product.product_tmpl_id
            if tmpl.toruq_use_custom_reserve:
                reserve = tmpl.toruq_reserve_qty
            else:
                reserve = company.toruq_safety_default
            reserve = max(0, reserve or 0)
        return real, reserve, max(0, real - reserve)

    @api.model
    def _toruq_upsert(self, company, products, sync_id, location_ids):
        qty_map = self._toruq_qty_map(products.ids, location_ids)
        existing = self.search([
            ('product_id', 'in', products.ids), ('company_id', '=', company.id)])
        existing_map = {rec.product_id.id: rec for rec in existing}
        to_create = []
        for product in products:
            real, reserve, salla_qty = self._toruq_calc(
                product, company, qty_map.get(product.id, 0.0))
            rec = existing_map.get(product.id)
            if rec is None:
                to_create.append({
                    'product_id': product.id,
                    'company_id': company.id,
                    'product_tmpl_id': product.product_tmpl_id.id,
                    'display_name': product.display_name,
                    'available_quantity': salla_qty,
                    'toruq_real_qty': real,
                    'toruq_reserve': reserve,
                    'merchant_id': company.tecfy_salla_merchant_id,
                    'is_posted': False,
                    'sync_id': sync_id,
                })
            else:
                vals = {'sync_id': sync_id, 'toruq_real_qty': real, 'toruq_reserve': reserve}
                if (rec.available_quantity != salla_qty
                        or rec.merchant_id != company.tecfy_salla_merchant_id):
                    vals.update({
                        'available_quantity': salla_qty,
                        'merchant_id': company.tecfy_salla_merchant_id,
                        'is_posted': False,
                        'toruq_fail_count': 0,
                        'error': False,
                    })
                rec.write(vals)
        if to_create:
            self.create(to_create)

    # ------------------------------------------------------------------
    # full cycle (Tecfy cron, every 20 min) - same flow, new qty rule
    # ------------------------------------------------------------------
    def prepareQuantities(self):
        companies = self.env['res.company'].search(
            [('tecfy_salla_update_quantity', '=', True)])
        for company in companies:
            location_ids = company.tecfy_salla_location_ids.ids
            if not location_ids:
                continue
            if not company.tecfy_salla_is_sync:
                company.tecfy_salla_is_sync = True
                company.tecfy_salla_sync_id = company.tecfy_salla_sync_id + 1
            sync_id = company.tecfy_salla_sync_id

            self._cr.execute(
                "select p.id from product_product p "
                "left join product_template t on t.id = p.product_tmpl_id "
                "where t.type = 'consu' and p.active = true and p.id not in "
                "(select product_id from tecfysalla_quantity "
                " where sync_id = %s and product_id is not null) limit %s;",
                (sync_id, FULL_BATCH))
            product_ids = [row['id'] for row in self._cr.dictfetchall()]
            products = self.env['product.product'].browse(product_ids).filtered(
                lambda p: not p.company_id or p.company_id.id == company.id)
            self._toruq_upsert(company, products, sync_id, location_ids)
            if len(product_ids) < FULL_BATCH:
                company.tecfy_salla_is_sync = False
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # post: failures are kept for retry (Tecfy marks everything as posted)
    # ------------------------------------------------------------------
    def tryPost(self):
        companies = self.env['res.company'].search(
            [('tecfy_salla_update_quantity', '=', True)])
        for company in companies:
            records = self.search([
                ('is_posted', '=', False),
                ('company_id', '=', company.id),
                ('toruq_fail_count', '<', MAX_FAILS),
            ], limit=POST_BATCH, order='id')
            if not records:
                continue
            data = [{
                'product_id': rec.product_id.id,
                'merchant_id': rec.merchant_id,
                'display_name': rec.display_name,
                'product_tmpl_id': rec.product_tmpl_id.id,
                'company_id': rec.company_id.id,
                'location_id': rec.location_id.id,
                'available_quantity': rec.available_quantity,
            } for rec in records]
            result = self.postToOdooNow({
                'merchant_id': company.tecfy_salla_merchant_id,
                'products': data,
            })
            if result.get('success'):
                records.write({'is_posted': True, 'error': False, 'toruq_fail_count': 0})
            else:
                message = (result.get('message') or 'Unknown error')[:250]
                _logger.warning('toruq_stock: Salla post failed: %s', message)
                for rec in records:
                    rec.write({'error': message,
                               'toruq_fail_count': rec.toruq_fail_count + 1})
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # fast cycle (every minute): only products whose stock moved
    # ------------------------------------------------------------------
    @api.model
    def toruq_fast_sync(self):
        icp = self.env['ir.config_parameter'].sudo()
        companies = self.env['res.company'].search([
            ('tecfy_salla_update_quantity', '=', True),
            ('toruq_fast_sync_enabled', '=', True)])
        for company in companies:
            location_ids = company.tecfy_salla_location_ids.ids
            if not location_ids:
                continue
            key = 'toruq_stock.last_fast_sync.%s' % company.id
            now = fields.Datetime.now()
            last_str = icp.get_param(key)
            last = (fields.Datetime.from_string(last_str) if last_str
                    else now - timedelta(minutes=5))
            since = last - timedelta(seconds=30)
            quants = self.env['stock.quant'].sudo().search([
                ('write_date', '>=', since), ('location_id', 'in', location_ids)])
            moves = self.env['stock.move'].sudo().search([
                ('write_date', '>=', since),
                '|', ('location_id', 'in', location_ids),
                ('location_dest_id', 'in', location_ids)])
            product_ids = set(quants.product_id.ids) | set(moves.product_id.ids)
            if product_ids:
                products = self.env['product.product'].browse(sorted(product_ids)).filtered(
                    lambda p: p.active and p.type == 'consu'
                    and (not p.company_id or p.company_id.id == company.id))
                self._toruq_upsert(
                    company, products, company.tecfy_salla_sync_id, location_ids)
            icp.set_param(key, fields.Datetime.to_string(now))
            self.env.cr.commit()
        self.tryPost()
