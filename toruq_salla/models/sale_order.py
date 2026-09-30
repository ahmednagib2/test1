from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    toruq_salla_store_id = fields.Many2one('toruq.salla.store', string='Salla Store', copy=False, index=True, readonly=True)
    toruq_salla_order_id = fields.Char(string='Salla Order ID', copy=False, index=True, readonly=True)
    toruq_salla_reference = fields.Char(string='Salla Reference', copy=False, readonly=True)
    toruq_salla_status = fields.Char(string='Salla Status', copy=False, readonly=True)
    toruq_salla_payment_method = fields.Char(string='Salla Payment Method', copy=False, readonly=True)

    _sql_constraints = [
        ('toruq_salla_order_uniq', 'unique(toruq_salla_store_id, toruq_salla_order_id)',
         'This Salla order is already imported.'),
    ]
