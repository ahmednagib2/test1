from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


class ProductTemplateTool(models.Model):
    _inherit = 'product.template'

    toruq_salla_sync_stock = fields.Boolean(string='Send Stock to Salla', default=True, copy=False)
    toruq_salla_max_qty = fields.Integer(
        string='Max Qty on Salla', copy=False,
        help='Never show more than this quantity in Salla. 0 means no limit.')
    toruq_salla_state = fields.Selection([
        ('off', 'Not sent'),
        ('pending', 'Waiting for sync'),
        ('synced', 'In Salla'),
        ('error', 'Error'),
    ], string='Salla Status', compute='_compute_toruq_salla_info')
    toruq_salla_qty = fields.Integer(string='Qty in Salla', compute='_compute_toruq_salla_info')
    toruq_salla_send_qty = fields.Integer(string='Qty to Send', compute='_compute_toruq_salla_info')
    toruq_salla_error = fields.Char(string='Salla Error', compute='_compute_toruq_salla_info')
    toruq_salla_last_push = fields.Datetime(string='Last Stock Push', compute='_compute_toruq_salla_info')

    @api.depends('toruq_salla_publish', 'product_variant_ids')
    def _compute_toruq_salla_info(self):
        Link = self.env['toruq.salla.product'].sudo()
        variant_ids = [i for i in self.mapped('product_variant_ids').ids if isinstance(i, int)]
        links = Link.search([('product_id', 'in', variant_ids)]) if variant_ids else Link
        by_tmpl = {}
        for link in links:
            by_tmpl.setdefault(link.product_id.product_tmpl_id.id, link)
        for tmpl in self:
            real_id = tmpl.id if isinstance(tmpl.id, int) else tmpl._origin.id
            link = by_tmpl.get(real_id)
            tmpl.toruq_salla_qty = link.salla_qty if link else 0
            tmpl.toruq_salla_send_qty = link.target_qty if link else 0
            tmpl.toruq_salla_error = (link.error or False) if link else False
            tmpl.toruq_salla_last_push = link.last_push_date if link else False
            if link and link.error:
                tmpl.toruq_salla_state = 'error'
            elif link and not link.missing_in_salla:
                tmpl.toruq_salla_state = 'synced'
            elif tmpl.toruq_salla_publish:
                tmpl.toruq_salla_state = 'pending'
            else:
                tmpl.toruq_salla_state = 'off'

    def write(self, vals):
        if 'toruq_salla_reserve' in vals and 'toruq_salla_reserve_custom' not in vals:
            vals = dict(vals, toruq_salla_reserve_custom=True)
        return super().write(vals)

    def _toruq_require_manager(self):
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))

    def _toruq_skuless(self):
        return self.filtered(lambda t: not any((v.default_code or '').strip() for v in t.product_variant_ids))

    def action_toruq_send_to_salla(self):
        self._toruq_require_manager()
        records = self.sudo()
        skuless = records._toruq_skuless()
        ready = records - skuless
        if ready:
            ready.write({'toruq_salla_publish': True})
            self.env['toruq.salla.store']._trigger_sync()
        message = _('%s products will be sent to Salla in the next sync (within a minute).') % len(ready)
        if skuless:
            message += ' ' + _('%(n)s skipped because they have no Internal Reference (SKU): %(names)s') % {
                'n': len(skuless), 'names': ', '.join(skuless[:5].mapped('name'))}
        return self.env['toruq.salla.store']._notify(message, 'warning' if skuless else 'success')

    def action_toruq_stop_sending(self):
        self._toruq_require_manager()
        self.sudo().write({'toruq_salla_publish': False})
        return self.env['toruq.salla.store']._notify(
            _('%s products will no longer be synced to Salla. They are not deleted from Salla.') % len(self))

    def action_toruq_open_options(self):
        self._toruq_require_manager()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Send to Salla - options'),
            'res_model': 'toruq.salla.send.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_product_tmpl_ids': [(6, 0, self.ids)]},
        }

    def action_toruq_push_stock_now(self):
        self._toruq_require_manager()
        Link = self.env['toruq.salla.product'].sudo()
        links = Link.search([
            ('product_id.product_tmpl_id', 'in', self.ids), ('missing_in_salla', '=', False)])
        total = 0
        for store in links.mapped('store_id'):
            total += store._push_stock(links.filtered(lambda l: l.store_id == store), compare='force')
        if not total:
            return self.env['toruq.salla.store']._notify(
                _('Nothing was sent. The products must exist in Salla, be selected, and have Send Stock enabled.'),
                'warning')
        return self.env['toruq.salla.store']._notify(_('Stock of %s products was sent to Salla.') % total)


class SallaSendWizard(models.TransientModel):
    _name = 'toruq.salla.send.wizard'
    _description = 'Send Products to Salla'

    product_tmpl_ids = fields.Many2many('product.template', string='Products')
    sync_stock = fields.Boolean(string='Send stock to Salla', default=True)
    set_reserve = fields.Boolean(string='Set a custom showroom reserve')
    reserve_qty = fields.Integer(string='Units kept for the showroom', default=1)
    set_max = fields.Boolean(string='Limit the quantity shown in Salla')
    max_qty = fields.Integer(string='Maximum quantity in Salla')
    push_now = fields.Boolean(string='Sync immediately', default=True)

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))
        records = self.product_tmpl_ids.sudo()
        skuless = records._toruq_skuless()
        ready = records - skuless
        if not ready:
            raise UserError(_('None of the selected products has an Internal Reference (SKU).'))
        vals = {'toruq_salla_publish': True, 'toruq_salla_sync_stock': self.sync_stock}
        if self.set_reserve:
            vals.update({'toruq_salla_reserve_custom': True, 'toruq_salla_reserve': max(0, self.reserve_qty)})
        if self.set_max:
            vals['toruq_salla_max_qty'] = max(0, self.max_qty)
        ready.write(vals)
        if self.push_now:
            self.env['toruq.salla.store']._trigger_sync()
        message = _('%s products selected for Salla.') % len(ready)
        if skuless:
            message += ' ' + _('%s skipped (no SKU).') % len(skuless)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Salla'), 'message': message,
                'type': 'warning' if skuless else 'success', 'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }


class SallaStoreTool(models.Model):
    _inherit = 'toruq.salla.store'

    def _targets_for_products(self, products):
        res = super()._targets_for_products(products)
        for product in products:
            cap = product.product_tmpl_id.toruq_salla_max_qty
            if cap and cap > 0 and product.id in res:
                avail, reserve, send = res[product.id]
                res[product.id] = (avail, reserve, min(send, cap))
        return res

    def _push_stock(self, links, compare='pushed'):
        def allowed(link):
            tmpl = link.product_id.product_tmpl_id
            return bool(link.product_id) and tmpl.toruq_salla_publish and tmpl.toruq_salla_sync_stock
        return super()._push_stock(links.filtered(allowed), compare=compare)
