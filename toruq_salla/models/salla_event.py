import json
import logging

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

_logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class SallaEvent(models.Model):
    _name = 'toruq.salla.event'
    _description = 'Salla Webhook Event'
    _order = 'id desc'
    _rec_name = 'event'

    store_id = fields.Many2one('toruq.salla.store', string='Store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True, index=True)
    event = fields.Char(string='Event', index=True)
    order_ref = fields.Char(string='Reference')
    payload = fields.Text(string='Payload')
    dedup_key = fields.Char(string='Dedup Key', index=True)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('ignored', 'Ignored'),
    ], string='Status', default='pending', index=True)
    attempts = fields.Integer(string='Attempts')
    next_try = fields.Datetime(string='Next Try', default=fields.Datetime.now)
    error = fields.Text(string='Error')
    received_at = fields.Datetime(string='Received At', default=fields.Datetime.now)
    processed_at = fields.Datetime(string='Processed At')
    sale_order_id = fields.Many2one('sale.order', string='Sales Order', ondelete='set null')

    _sql_constraints = [
        ('dedup_uniq', 'unique(store_id, dedup_key)', 'This event was already received.'),
    ]

    def _process(self):
        self.ensure_one()
        store = self.store_id.sudo()
        payload = json.loads(self.payload or '{}')
        data = payload.get('data') or {}
        name = self.event
        if name == 'order.created':
            order = store._import_order(payload)
            if order:
                self.sale_order_id = order.id
        elif name in ('order.updated', 'order.status.updated', 'order.payment.updated'):
            store._update_order(payload)
        elif name == 'order.cancelled':
            store._cancel_by_payload(payload)
        elif name == 'order.refunded':
            store._note_refund(payload)
        elif name in ('product.created', 'product.price.updated', 'product.status.updated'):
            if data.get('id'):
                store._upsert_mapping(data)
        elif name == 'product.deleted':
            store._mark_deleted(data)
        elif name == 'app.uninstalled':
            store.write({'state': 'error', 'last_error': _('The Salla app was uninstalled from the store.')})
        else:
            return False
        return True

    @api.model
    def _process_store(self, store, limit=50):
        events = self.search([
            ('store_id', '=', store.id),
            ('state', 'in', ('pending', 'failed')),
            ('attempts', '<', MAX_ATTEMPTS),
            ('next_try', '<=', fields.Datetime.now()),
        ], order='id', limit=limit)
        for ev in events:
            try:
                with self.env.cr.savepoint():
                    handled = ev._process()
                ev.write({
                    'state': 'done' if handled else 'ignored',
                    'processed_at': fields.Datetime.now(),
                    'error': False,
                })
            except Exception as exc:
                _logger.warning('toruq_salla: event %s failed: %s', ev.id, exc)
                attempts = ev.attempts + 1
                ev.write({
                    'state': 'failed',
                    'attempts': attempts,
                    'error': str(exc)[:2000],
                    'next_try': fields.Datetime.add(fields.Datetime.now(), minutes=2 * attempts),
                })
            self.env.cr.commit()
        return len(events)

    def _check_manager(self):
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))

    def action_retry(self):
        self._check_manager()
        self.sudo().write({
            'state': 'pending', 'attempts': 0, 'error': False,
            'next_try': fields.Datetime.now(),
        })
        return True
