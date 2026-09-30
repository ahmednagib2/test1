from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    toruq_use_custom_reserve = fields.Boolean(
        string='Custom Salla reserve / احتياطي خاص')
    toruq_reserve_qty = fields.Integer(
        string='Showroom reserve qty / احتياطي المعرض', default=1)
    toruq_salla_qty = fields.Integer(
        string='Qty sent to Salla / كمية سلة', compute='_compute_toruq_salla_qty')

    @api.depends('product_variant_ids')
    def _compute_toruq_salla_qty(self):
        for tmpl in self:
            tmpl.toruq_salla_qty = sum(tmpl.product_variant_ids.mapped('toruq_salla_qty'))


class ProductProduct(models.Model):
    _inherit = 'product.product'

    toruq_real_qty = fields.Integer(
        string='Odoo real qty (Salla locations)', compute='_compute_toruq_salla_qty')
    toruq_salla_qty = fields.Integer(
        string='Qty sent to Salla / كمية سلة', compute='_compute_toruq_salla_qty')

    def _compute_toruq_salla_qty(self):
        company = self.env.company
        Qty = self.env['tecfysalla.quantity']
        ids = [i for i in self.ids if isinstance(i, int)]
        qty_map = Qty._toruq_qty_map(ids, company.tecfy_salla_location_ids.ids)
        for product in self:
            real, _reserve, salla = Qty._toruq_calc(
                product, company, qty_map.get(product.id, 0.0))
            product.toruq_real_qty = real
            product.toruq_salla_qty = salla
