from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    toruq_salla_customer_id = fields.Char(string='Salla Customer ID', copy=False, index=True)
