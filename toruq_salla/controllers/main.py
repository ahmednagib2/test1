import hashlib
import json
import logging

from odoo import http
from odoo.http import Response, request

_logger = logging.getLogger(__name__)

HANDLED_EVENTS = {
    'app.store.authorize',
    'app.installed',
    'app.updated',
    'app.uninstalled',
    'order.created',
    'order.updated',
    'order.status.updated',
    'order.payment.updated',
    'order.cancelled',
    'order.refunded',
    'product.created',
    'product.updated',
    'product.deleted',
    'product.price.updated',
    'product.status.updated',
}


def _reply(status, message):
    return Response(
        json.dumps({'ok': status == 200, 'message': message}),
        status=status,
        headers=[('Content-Type', 'application/json')],
    )


class SallaWebhook(http.Controller):

    @http.route('/toruq_salla/webhook', type='http', auth='public', methods=['GET', 'POST'], csrf=False)
    def webhook(self, **kwargs):
        if request.httprequest.method == 'GET':
            return Response(
                'toruq_salla webhook is ready',
                status=200,
                headers=[('Content-Type', 'text/plain')],
            )

        raw = request.httprequest.get_data() or b''
        try:
            payload = json.loads(raw.decode('utf-8'))
        except ValueError:
            return _reply(400, 'invalid json')
        if not isinstance(payload, dict):
            return _reply(400, 'invalid payload')

        merchant = str(payload.get('merchant') or '')
        event = payload.get('event') or ''
        signature = request.httprequest.headers.get('X-Salla-Signature', '')
        Store = request.env['toruq.salla.store'].sudo()

        # Prefer a store matched by merchant and signature. During the first
        # Easy Mode authorization merchant_id may not be stored yet, so fall
        # back to the only draft store with the same webhook secret.
        store = Store._match_store(merchant, raw, signature)
        if not store:
            candidates = Store.search([
                ('state', '=', 'draft'),
                ('webhook_secret', '!=', False),
            ])
            valid = candidates.filtered(lambda s: s._webhook_signature_valid(raw, signature))
            if len(valid) == 1:
                store = valid

        if not store:
            return _reply(401, 'invalid signature or store')

        data = payload.get('data') or {}
        if merchant and not store.merchant_id:
            store.sudo().write({'merchant_id': merchant})

        if event == 'app.store.authorize':
            store._handle_authorize(payload)
            return _reply(200, 'authorized')

        if event not in HANDLED_EVENTS:
            return _reply(200, 'ignored')

        dedup = hashlib.sha256(raw).hexdigest()
        Event = request.env['toruq.salla.event'].sudo()
        if not Event.search([('store_id', '=', store.id), ('dedup_key', '=', dedup)], limit=1):
            Event.create({
                'store_id': store.id,
                'event': event,
                'order_ref': str(data.get('reference_id') or data.get('id') or ''),
                'payload': raw.decode('utf-8'),
                'dedup_key': dedup,
            })
            try:
                cron = request.env.ref('toruq_salla.ir_cron_sync', raise_if_not_found=False)
                if cron:
                    cron.sudo()._trigger()
            except Exception:
                _logger.debug('toruq_salla: could not trigger cron', exc_info=True)
        return _reply(200, 'queued')
