import json

from odoo import _, models

from .salla_store import SallaFatalError


class SallaStoreGuard(models.Model):
    _inherit = 'toruq.salla.store'

    def _has_stock_source(self):
        self.ensure_one()
        return bool(self._get_location_ids())

    def _no_source_message(self):
        return _('Stock was not sent to Salla: choose a Stock Warehouse or Stock Locations on the store first.')

    def _push_stock(self, links, compare='pushed'):
        self.ensure_one()
        if not self._has_stock_source():
            message = self._no_source_message()
            if self.last_error != message:
                self.sudo().write({'last_error': message})
            return 0
        return super()._push_stock(links, compare=compare)

    def _create_in_salla(self, product):
        self.ensure_one()
        if not self._has_stock_source():
            raise SallaFatalError(self._no_source_message())
        return super()._create_in_salla(product)

    def _sync_products(self):
        self.ensure_one()
        result = super()._sync_products()
        untracked = self._published_products().filtered(
            lambda p: p.type == 'consu' and not p.is_storable)
        if untracked:
            note = _('%(n)s published products do not have Track Inventory enabled, so their stock is not sent to Salla: %(names)s') % {
                'n': len(untracked), 'names': ', '.join(untracked[:5].mapped('display_name'))}
            current = self.last_error or ''
            if note not in current:
                self.sudo().write({'last_error': ('%s\n%s' % (current, note)).strip()[:1500]})
                self.env.cr.commit()
        return result


class SallaEventGuard(models.Model):
    _inherit = 'toruq.salla.event'

    def _process(self):
        self.ensure_one()
        if self.event == 'product.updated':
            payload = json.loads(self.payload or '{}')
            data = payload.get('data') or {}
            if data.get('id'):
                self.store_id.sudo()._upsert_mapping(data)
            return True
        return super()._process()
