from odoo import _, fields, models
from odoo.exceptions import AccessError


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    toruq_salla_publish = fields.Boolean(string='Publish on Salla', copy=False, index=True)
    toruq_salla_reserve_custom = fields.Boolean(string='Custom Showroom Reserve', copy=False)
    toruq_salla_reserve = fields.Integer(string='Showroom Reserve Qty', default=1, copy=False)

    def _toruq_check_manager(self):
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))

    def toruq_action_publish(self):
        self._toruq_check_manager()
        self.sudo().write({'toruq_salla_publish': True})
        return True

    def toruq_action_unpublish(self):
        self._toruq_check_manager()
        self.sudo().write({'toruq_salla_publish': False})
        return True
