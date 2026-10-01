import logging

from odoo import _, api, models

_logger = logging.getLogger(__name__)


class SallaStoreStockSync(models.Model):
    _inherit = 'toruq.salla.store'

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'safety_enabled' in fields_list:
            res['safety_enabled'] = True
        if 'safety_default' in fields_list and not res.get('safety_default'):
            res['safety_default'] = 1
        return res

    @api.model
    def _trigger_sync(self):
        cron = self.env.ref('toruq_salla.ir_cron_sync', raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    @staticmethod
    def _sku_variants(sku):
        sku = (sku or '').strip()
        base = sku.lstrip('-').strip()
        variants = []
        for value in (sku, base, '-' + base if base else '', sku.replace(' ', ''), base.replace(' ', '')):
            if value and value not in variants:
                variants.append(value)
        return variants

    def _find_product(self, sku):
        self.ensure_one()
        product = super()._find_product(sku)
        if product:
            return product
        variants = self._sku_variants(sku)
        if not variants:
            return product
        Product = self.env['product.product'].sudo()
        company_domain = ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)]
        for field_name in ('default_code', 'barcode'):
            found = Product.search([(field_name, 'in', variants)] + company_domain, limit=2)
            if len(found) == 1:
                return found
        return product

    def action_relink_all(self):
        self._check_manager()
        relinked = 0
        Map = self.env['toruq.salla.product'].sudo()
        for store in self:
            links = Map.search([
                ('store_id', '=', store.id), ('product_id', '=', False), ('missing_in_salla', '=', False)])
            for link in links:
                product = store._find_product(link.sku) if link.sku else self.env['product.product']
                if product:
                    link.write({'product_id': product.id, 'link_state': 'linked'})
                    relinked += 1
        self._trigger_sync()
        return self._notify(_('%(n)s Salla products were linked to Odoo products by SKU or barcode.') % {'n': relinked})

    def action_publish_all(self):
        self._check_manager()
        Template = self.env['product.template'].sudo()
        total = 0
        for store in self:
            templates = Template.search([
                ('sale_ok', '=', True), ('type', '=', 'consu'), ('is_storable', '=', True),
                ('default_code', '!=', False), ('toruq_salla_publish', '=', False),
                '|', ('company_id', '=', False), ('company_id', '=', store.company_id.id),
            ])
            templates.write({'toruq_salla_publish': True})
            total += len(templates)
        self._trigger_sync()
        return self._notify(_(
            '%s products were marked as published on Salla. New products are created in Salla with the status chosen on the store (hidden by default).'
        ) % total)


class StockMove(models.Model):
    _inherit = 'stock.move'

    def _action_done(self, *args, **kwargs):
        moves = super()._action_done(*args, **kwargs)
        try:
            product_ids = moves.mapped('product_id').ids
            if product_ids and self.env['toruq.salla.product'].sudo().search(
                    [('product_id', 'in', product_ids)], limit=1):
                self.env['toruq.salla.store']._trigger_sync()
        except Exception:
            _logger.debug('toruq_salla: could not trigger stock sync', exc_info=True)
        return moves
