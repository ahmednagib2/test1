from odoo import _, api, fields, models
from odoo.exceptions import AccessError


class SallaProduct(models.Model):
    _name = 'toruq.salla.product'
    _description = 'Salla Product Link'
    _order = 'store_id, sku, id'
    _rec_name = 'name'

    store_id = fields.Many2one('toruq.salla.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)
    product_id = fields.Many2one('product.product', string='Odoo Product', index=True, ondelete='set null')
    salla_product_id = fields.Char(string='Salla Product ID', required=True, index=True, copy=False)
    sku = fields.Char(string='SKU', index=True)
    name = fields.Char(string='Salla Name')
    salla_status = fields.Char(string='Salla Status')
    salla_type = fields.Char(string='Salla Type')
    salla_price = fields.Float(string='Salla Price')
    salla_qty = fields.Integer(string='Salla Stock')
    salla_unlimited = fields.Boolean(string='Unlimited in Salla')
    link_state = fields.Selection([
        ('linked', 'Linked'),
        ('unmatched', 'Not in Odoo'),
        ('missing', 'Missing in Salla'),
    ], string='Link Status', default='unmatched', index=True)
    missing_in_salla = fields.Boolean(string='Missing in Salla', index=True)
    exclude = fields.Boolean(string='Exclude from Sync')
    last_seen = fields.Datetime(string='Last Seen in Salla')
    last_pushed_qty = fields.Integer(string='Last Pushed Qty', default=-1)
    last_push_date = fields.Datetime(string='Last Stock Push')
    last_push_attempt = fields.Datetime(string='Last Push Attempt')
    fail_count = fields.Integer(string='Failed Pushes')
    error = fields.Char(string='Error')
    data_hash = fields.Char(string='Data Hash')
    odoo_qty = fields.Integer(string='Odoo Available', compute='_compute_qty')
    reserve_qty = fields.Integer(string='Showroom Reserve', compute='_compute_qty')
    target_qty = fields.Integer(string='Qty to Send', compute='_compute_qty')

    _sql_constraints = [
        ('store_salla_uniq', 'unique(store_id, salla_product_id)', 'This Salla product is already linked.'),
    ]

    @api.depends('store_id', 'product_id')
    def _compute_qty(self):
        for store in self.mapped('store_id'):
            recs = self.filtered(lambda r: r.store_id == store)
            targets = store._compute_targets(recs.filtered('product_id'))
            for rec in recs:
                real, reserve, target = targets.get(rec.id, (0, 0, 0))
                rec.odoo_qty, rec.reserve_qty, rec.target_qty = real, reserve, target
        for rec in self.filtered(lambda r: not r.store_id):
            rec.odoo_qty = rec.reserve_qty = rec.target_qty = 0

    def _check_manager(self):
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))

    def action_push_stock(self):
        self._check_manager()
        total = 0
        for store in self.mapped('store_id'):
            recs = self.filtered(lambda r: r.store_id == store).sudo()
            total += store.sudo()._push_stock(recs, compare='force')
        return self.env['toruq.salla.store']._notify(_('%s products updated in Salla.') % total)

    def action_relink(self):
        self._check_manager()
        for rec in self.sudo():
            product = rec.store_id._find_product(rec.sku) if rec.sku else self.env['product.product']
            rec.write({
                'product_id': product.id or False,
                'link_state': 'linked' if product else 'unmatched',
            })
        return True
