from odoo import api, fields, models

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    salla_sync_enabled = fields.Boolean(string='Sync with Salla')
    salla_store_id = fields.Many2one('toruq.salla.store', string='Salla Store')
    salla_product_id = fields.Char(string='Salla Product ID', copy=False, readonly=True)
    salla_sync_price = fields.Boolean(string='Sync Price', default=True)
    salla_sync_stock = fields.Boolean(string='Sync Stock', default=True)
    salla_sync_status = fields.Selection([
        ('not_synced', 'Not Synced'), ('synced', 'Synced'), ('error', 'Error')
    ], default='not_synced', copy=False)
    salla_sync_message = fields.Text(string='Sync Message', copy=False)
    salla_last_sync = fields.Datetime(string='Last Sync', copy=False)
