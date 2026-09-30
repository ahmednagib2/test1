from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    toruq_safety_enabled = fields.Boolean(
        string='Enable showroom safety stock / تفعيل احتياطي المعرض',
        default=False,
        help='When enabled, Salla receives: max(0, Odoo available qty - reserve).')
    toruq_safety_default = fields.Integer(
        string='Default showroom reserve / الاحتياطي الافتراضي',
        default=1)
    toruq_fast_sync_enabled = fields.Boolean(
        string='Fast sync (every minute) / مزامنة سريعة',
        default=True,
        help='Only products whose stock moved recently are recalculated and posted.')
