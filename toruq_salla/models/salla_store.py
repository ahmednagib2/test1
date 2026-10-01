import hashlib
import hmac
import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

import requests

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

API_BASE = 'https://api.salla.dev/admin/v2'
TOKEN_URL = 'https://accounts.salla.sa/oauth2/token'
TIMEOUT = 30
PAGE_SIZE = 50
PUSH_CHUNK = 100
MAX_CHUNKS = 20
CREATE_PER_RUN = 15
UPDATE_PER_RUN = 30
MAX_FAILS = 5
CANCEL_SLUGS = {'canceled', 'cancelled'}
REQUIRED_SCOPE = 'products.read_write'


class SallaFatalError(UserError):
    """Errors that make every further call useless (auth, scope, rate limit, network)."""


def _money(value):
    if isinstance(value, dict):
        value = value.get('amount')
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _csv(text):
    return {p.strip().lower() for p in (text or '').split(',') if p.strip()}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _status_slug(data):
    status = data.get('status')
    if isinstance(status, dict):
        return (status.get('slug') or status.get('name') or '').strip().lower()
    return str(status or '').strip().lower()


class SallaStore(models.Model):
    _name = 'toruq.salla.store'
    _description = 'Salla Store'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(string='Store Name', required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                 default=lambda self: self.env.company)
    merchant_id = fields.Char(string='Salla Merchant ID', copy=False, index=True)
    store_name = fields.Char(string='Salla Store Name', readonly=True, copy=False)
    state = fields.Selection([
        ('draft', 'Not Connected'),
        ('connected', 'Connected'),
        ('error', 'Connection Error'),
    ], string='Status', default='draft', readonly=True, copy=False, tracking=True)
    last_error = fields.Text(string='Last Error', readonly=True, copy=False)

    client_id = fields.Char(string='Client ID', copy=False, groups='toruq_salla.group_manager')
    client_secret = fields.Char(string='Client Secret', copy=False, groups='toruq_salla.group_manager')
    webhook_secret = fields.Char(string='Webhook Secret', copy=False, groups='toruq_salla.group_manager')
    access_token = fields.Char(string='Access Token', copy=False, groups='toruq_salla.group_manager')
    refresh_token = fields.Char(string='Refresh Token', copy=False, groups='toruq_salla.group_manager')
    token_expires = fields.Datetime(string='Token Expires (UTC)', copy=False)
    granted_scope = fields.Char(string='Granted Scopes', readonly=True, copy=False)
    webhook_url = fields.Char(string='Webhook URL', compute='_compute_webhook_url')

    warehouse_id = fields.Many2one('stock.warehouse', string='Stock Warehouse',
                                   domain="[('company_id', '=', company_id)]")
    location_ids = fields.Many2many('stock.location', 'toruq_salla_store_location_rel', 'store_id', 'location_id',
                                    string='Stock Locations', domain=[('usage', '=', 'internal')])
    stock_sync_enabled = fields.Boolean(string='Sync Stock to Salla', default=True)
    safety_enabled = fields.Boolean(string='Keep a Showroom Reserve')
    safety_default = fields.Integer(string='Default Showroom Reserve', default=1)
    branch_ref = fields.Char(string='Salla Branch ID (optional)')

    auto_create_products = fields.Boolean(string='Create Published Products in Salla', default=True)
    new_product_status = fields.Selection([
        ('hidden', 'Hidden'),
        ('sale', 'On Sale'),
    ], string='Status of New Products', default='hidden')
    sync_name = fields.Boolean(string='Sync Product Names', default=True)
    name_lang_id = fields.Many2one('res.lang', string='Name Language', domain=[('active', '=', True)])
    sync_price = fields.Boolean(string='Sync Prices', default=True)
    price_tax_mode = fields.Selection([
        ('as_is', 'Odoo sales price as is'),
        ('add_tax', 'Add sales taxes to the price'),
    ], string='Price Mode', default='as_is')
    sync_description = fields.Boolean(string='Sync Descriptions')

    order_import_mode = fields.Selection([
        ('none', 'Do not import orders'),
        ('quotation', 'Create quotations'),
        ('confirmed', 'Create confirmed sales orders'),
    ], string='Order Import', default='quotation', required=True)
    unmatched_policy = fields.Selection([
        ('error', 'Fail and retry after linking'),
        ('note', 'Add a note line'),
    ], string='Unknown Products in Orders', default='error', required=True)
    salesperson_id = fields.Many2one('res.users', string='Salesperson')
    shipping_product_id = fields.Many2one('product.product', string='Shipping Product')
    fee_product_id = fields.Many2one('product.product', string='Cash on Delivery Fee Product')
    auto_invoice = fields.Boolean(string='Create Invoices Automatically')
    invoice_statuses = fields.Char(string='Invoice on Salla Statuses', default='in_progress,shipped,delivered,completed')
    auto_payment = fields.Boolean(string='Register Payments Automatically')
    payment_journal_id = fields.Many2one('account.journal', string='Payment Journal',
                                         domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]")
    auto_deliver = fields.Boolean(string='Validate Deliveries Automatically')
    deliver_statuses = fields.Char(string='Deliver on Salla Statuses', default='delivered,completed')

    last_pull_date = fields.Datetime(string='Last Pull from Salla', readonly=True, copy=False)
    last_push_date = fields.Datetime(string='Last Stock Push', readonly=True, copy=False)

    _sql_constraints = [
        ('merchant_uniq', 'unique(merchant_id)', 'This Salla merchant is already linked to another store.'),
    ]

    @api.depends('name')
    def _compute_webhook_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url') or ''
        for store in self:
            store.webhook_url = base.rstrip('/') + '/toruq_salla/webhook'

    @api.constrains('stock_sync_enabled', 'order_import_mode')
    def _check_stock_needs_orders(self):
        for store in self:
            if store.stock_sync_enabled and store.order_import_mode == 'none':
                raise ValidationError(_('Stock sync needs order import. Otherwise Salla stock would be reset by Odoo.'))

    def _check_manager(self):
        if not self.env.user.has_group('toruq_salla.group_manager'):
            raise AccessError(_('Only Salla managers can do this.'))

    @api.model
    def _notify(self, message, kind='success'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': _('Salla'), 'message': message, 'type': kind, 'sticky': kind != 'success'},
        }

    def _set_error(self, message):
        self.sudo().write({'state': 'error', 'last_error': message})

    # ------------------------------------------------------------------
    # OAuth and API
    # ------------------------------------------------------------------
    @api.model
    def _parse_expiry(self, data):
        exp = data.get('expires')
        try:
            if exp:
                return datetime.fromtimestamp(int(exp), tz=timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        try:
            secs = data.get('expires_in')
            if secs:
                return _utcnow() + timedelta(seconds=int(secs))
        except (TypeError, ValueError):
            pass
        return _utcnow() + timedelta(days=14)

    def _token_valid(self):
        self.ensure_one()
        if not self.access_token:
            return False
        return not self.token_expires or self.token_expires > _utcnow() + timedelta(hours=12)

    def _refresh_access_token(self):
        self.ensure_one()
        # Lock the row so two workers never use the same one-time refresh token.
        self.env.cr.execute('SELECT id FROM toruq_salla_store WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['access_token', 'refresh_token', 'token_expires'])
        if self._token_valid():
            return True
        if not (self.refresh_token and self.client_id and self.client_secret):
            message = _('Cannot refresh the Salla token: refresh token or client credentials are missing. Re-install the app from Salla.')
            self._set_error(message)
            raise SallaFatalError(message)
        try:
            resp = requests.post(TOKEN_URL, data={
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token,
                'client_id': self.client_id,
                'client_secret': self.client_secret,
            }, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SallaFatalError(_('Could not reach Salla: %s') % exc)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not resp.ok or not data.get('access_token'):
            message = _('Salla refused to refresh the token: %s') % (self._error_text(resp) or resp.status_code)
            self._set_error(message)
            self.env.cr.commit()
            raise SallaFatalError(message)
        vals = {
            'access_token': data['access_token'],
            'refresh_token': data.get('refresh_token') or self.refresh_token,
            'token_expires': self._parse_expiry(data),
            'state': 'connected',
            'last_error': False,
        }
        scope = self._scope_text(data)
        if scope:
            vals['granted_scope'] = scope
        self.write(vals)
        # Persist immediately: the old refresh token is now useless.
        self.env.cr.commit()
        return True

    @api.model
    def _scope_text(self, data):
        scope = data.get('scope') or data.get('scopes') or ''
        if isinstance(scope, (list, tuple)):
            scope = ' '.join(str(s) for s in scope)
        return str(scope).strip()

    @api.model
    def _error_text(self, resp):
        try:
            body = resp.json()
        except ValueError:
            return (resp.text or '')[:300]
        err = body.get('error')
        if isinstance(err, dict):
            text = err.get('message') or err.get('code') or ''
            fields_err = err.get('fields')
            if fields_err:
                text = '%s %s' % (text, json.dumps(fields_err, ensure_ascii=False))
            return text[:500]
        return str(err or body.get('message') or body.get('error_description') or '')[:500]

    def _api(self, method, path, params=None, payload=None, _retried=False):
        self.ensure_one()
        store = self.sudo()
        if not store.access_token:
            raise SallaFatalError(_('Salla is not connected yet. Install the app on the store (Easy Mode) or paste an access token.'))
        if not store._token_valid() and store.refresh_token:
            store._refresh_access_token()
        headers = {
            'Authorization': 'Bearer %s' % store.access_token,
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }
        try:
            resp = requests.request(method, API_BASE + path, headers=headers, params=params,
                                    json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise SallaFatalError(_('Could not reach Salla: %s') % exc)
        if resp.status_code == 401 and not _retried and store.refresh_token:
            store.write({'token_expires': _utcnow() - timedelta(minutes=1)})
            store._refresh_access_token()
            return store._api(method, path, params=params, payload=payload, _retried=True)
        if resp.status_code in (401, 403):
            raise SallaFatalError(_(
                'Salla refused access (%(code)s): %(msg)s. The app token probably lacks the %(scope)s scope. '
                'Enable that scope in the Salla app, re-install/re-authorize the app on the store, then press Test Connection.'
            ) % {'code': resp.status_code, 'msg': self._error_text(resp) or '-', 'scope': REQUIRED_SCOPE})
        if resp.status_code == 429:
            raise SallaFatalError(_('Salla rate limit reached. It will be retried automatically.'))
        if not resp.ok:
            raise UserError(_('Salla API error %(code)s: %(msg)s') % {
                'code': resp.status_code, 'msg': self._error_text(resp)})
        try:
            return resp.json()
        except ValueError:
            return {}

    def action_test_connection(self):
        self._check_manager()
        warnings = []
        for store in self:
            res = store._api('GET', '/store/info')
            data = res.get('data') or {}
            vals = {'state': 'connected', 'last_error': False}
            if data.get('name'):
                vals['store_name'] = data['name']
            if data.get('id') and not store.merchant_id:
                vals['merchant_id'] = str(data['id'])
            store.sudo().write(vals)
            try:
                store._api('GET', '/products', params={'page': 1, 'per_page': 1})
            except UserError as exc:
                store.sudo().write({'last_error': str(exc)[:500]})
                warnings.append('%s: %s' % (store.name, exc))
        if warnings:
            self.env.cr.commit()
            return self._notify(_('Connected, but product access failed. %s') % ' | '.join(warnings), 'warning')
        return self._notify(_('Connection to Salla and product access are working.'))

    def _handle_authorize(self, payload):
        self.ensure_one()
        data = payload.get('data') or {}
        if not data.get('access_token'):
            return False
        vals = {
            'access_token': data['access_token'],
            'token_expires': self._parse_expiry(data),
            'state': 'connected',
            'last_error': False,
        }
        if data.get('refresh_token'):
            vals['refresh_token'] = data['refresh_token']
        scope = self._scope_text(data)
        if scope:
            vals['granted_scope'] = scope
            if REQUIRED_SCOPE not in scope:
                vals['last_error'] = _('The token was issued without the %s scope. Enable it in the Salla app and re-authorize.') % REQUIRED_SCOPE
        merchant = str(payload.get('merchant') or '')
        if merchant and not self.merchant_id:
            vals['merchant_id'] = merchant
        self.sudo().write(vals)
        self.sudo().message_post(body=_('Salla access token received and stored.'))
        return True

    def _webhook_signature_valid(self, raw, signature):
        self.ensure_one()
        secret = (self.sudo().webhook_secret or '').encode('utf-8')
        sig = (signature or '').strip().lower()
        if sig.startswith('sha256='):
            sig = sig[7:]
        if not secret or not sig:
            return False
        expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)

    @api.model
    def _match_store(self, merchant, raw, signature):
        stores = self.search([('merchant_id', '=', merchant)]) if merchant else self.browse()
        stores |= self.search([('merchant_id', '=', False)])
        for store in stores:
            if store._webhook_signature_valid(raw, signature):
                return store
        return self.browse()

    # ------------------------------------------------------------------
    # Products: pull from Salla and link by SKU
    # ------------------------------------------------------------------
    def _find_product(self, sku):
        self.ensure_one()
        sku = (sku or '').strip()
        if not sku:
            return self.env['product.product']
        link = self.env['toruq.salla.product'].sudo().search([
            ('store_id', '=', self.id), ('sku', '=', sku), ('product_id', '!=', False)], limit=1)
        if link:
            return link.product_id
        return self.env['product.product'].sudo().search([
            ('default_code', '=', sku),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ], limit=1)

    def _upsert_mapping(self, item):
        self.ensure_one()
        Map = self.env['toruq.salla.product'].sudo()
        sid = str(item.get('id') or '')
        if not sid:
            return Map
        rec = Map.search([('store_id', '=', self.id), ('salla_product_id', '=', sid)], limit=1)
        vals = {'store_id': self.id, 'salla_product_id': sid,
                'last_seen': fields.Datetime.now(), 'missing_in_salla': False}
        if 'sku' in item:
            vals['sku'] = (item.get('sku') or '').strip()
        if 'name' in item:
            vals['name'] = item.get('name') or ''
        if 'status' in item:
            vals['salla_status'] = item.get('status') or ''
        if item.get('type') or item.get('product_type'):
            vals['salla_type'] = item.get('type') or item.get('product_type')
        if 'price' in item:
            vals['salla_price'] = _money(item.get('price'))
        if 'quantity' in item:
            if item.get('quantity') is None:
                vals.update({'salla_unlimited': True, 'salla_qty': 0})
            else:
                try:
                    vals.update({'salla_unlimited': False, 'salla_qty': int(float(item.get('quantity')))})
                except (TypeError, ValueError):
                    pass
        sku = vals.get('sku', rec.sku if rec else '')
        product = rec.product_id if rec else self.env['product.product']
        if not product and sku:
            product = self._find_product(sku)
        vals['product_id'] = product.id or False
        vals['link_state'] = 'linked' if product else 'unmatched'
        if rec:
            rec.write(vals)
            return rec
        return Map.create(vals)

    def _mark_deleted(self, data):
        self.ensure_one()
        sid = str(data.get('id') or '')
        if sid:
            self.env['toruq.salla.product'].sudo().search([
                ('store_id', '=', self.id), ('salla_product_id', '=', sid)
            ]).write({'missing_in_salla': True, 'link_state': 'missing'})

    def _pull_products(self):
        self.ensure_one()
        started = fields.Datetime.now()
        page, pages, count = 1, 1, 0
        while page <= pages:
            res = self._api('GET', '/products', params={'page': page, 'per_page': PAGE_SIZE})
            for item in res.get('data') or []:
                self._upsert_mapping(item)
                count += 1
            pagination = res.get('pagination') or {}
            pages = int(pagination.get('totalPages') or pagination.get('total_pages') or 1)
            page += 1
            if page <= pages:
                time.sleep(0.3)
        self.env['toruq.salla.product'].sudo().search([
            ('store_id', '=', self.id), ('last_seen', '<', started),
        ]).write({'missing_in_salla': True, 'link_state': 'missing'})
        self.sudo().write({'last_pull_date': fields.Datetime.now(), 'last_error': False})
        return count

    def action_pull_products(self):
        self._check_manager()
        total = 0
        for store in self:
            total += store._pull_products()
        return self._notify(_('%s products loaded from Salla.') % total)

    # ------------------------------------------------------------------
    # Products: publish selected Odoo products to Salla
    # ------------------------------------------------------------------
    def _product_texts(self, product):
        lang = self.name_lang_id.code if self.name_lang_id else (self.env.lang or 'en_US')
        p = product.with_context(lang=lang)
        name = p.name or ''
        if p.product_tmpl_id.product_variant_count > 1 and p.product_template_variant_value_ids:
            name = '%s (%s)' % (name, p.product_template_variant_value_ids._get_combination_name())
        return p, name, (p.description_sale or '').strip()

    def _product_price(self, p):
        price = p.lst_price
        if self.price_tax_mode == 'add_tax':
            taxes = p.taxes_id.filtered(lambda t: t.company_id == self.company_id)
            if taxes:
                price = taxes.compute_all(price, currency=self.company_id.currency_id,
                                          quantity=1.0, product=p)['total_included']
        return round(price, 2)

    def _product_payload(self, product):
        self.ensure_one()
        p, name, description = self._product_texts(product)
        payload = {'name': name, 'price': self._product_price(p)}
        if self.sync_description and description:
            payload['description'] = description
        return payload

    def _data_hash(self, payload):
        keep = {}
        if self.sync_name:
            keep['name'] = payload.get('name')
        if self.sync_price:
            keep['price'] = payload.get('price')
        if self.sync_description:
            keep['description'] = payload.get('description')
        return hashlib.sha1(json.dumps(keep, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    def _published_products(self):
        self.ensure_one()
        return self.env['product.product'].sudo().search([
            ('product_tmpl_id.toruq_salla_publish', '=', True),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id),
        ])

    def _create_in_salla(self, product):
        self.ensure_one()
        payload = self._product_payload(product)
        payload.update({
            'product_type': 'product',
            'sku': product.default_code,
            'status': self.new_product_status or 'hidden',
        })
        if 'description' not in payload:
            payload['description'] = payload['name']
        if product.is_storable:
            payload['quantity'] = self._targets_for_products(product)[product.id][2]
        res = self._api('POST', '/products', payload=payload)
        data = res.get('data') or {}
        sid = str(data.get('id') or '')
        if not sid:
            raise UserError(_('Salla did not return a product id.'))
        vals = {
            'store_id': self.id, 'product_id': product.id, 'salla_product_id': sid,
            'sku': product.default_code, 'name': payload['name'],
            'salla_status': data.get('status') or payload['status'], 'salla_type': 'product',
            'salla_price': payload['price'], 'link_state': 'linked',
            'last_seen': fields.Datetime.now(), 'data_hash': self._data_hash(payload),
        }
        if 'quantity' in payload:
            vals.update({'salla_qty': payload['quantity'], 'last_pushed_qty': payload['quantity']})
        return self.env['toruq.salla.product'].sudo().create(vals)

    def _update_in_salla(self, link):
        self.ensure_one()
        payload = self._product_payload(link.product_id)
        payload_hash = self._data_hash(payload)
        if payload_hash == link.data_hash or not (self.sync_name or self.sync_price or self.sync_description):
            return False
        body = {'name': payload['name'] if self.sync_name else (link.name or payload['name'])}
        if self.sync_price:
            body['price'] = payload['price']
        if self.sync_description and payload.get('description'):
            body['description'] = payload['description']
        self._api('PUT', '/products/%s' % link.salla_product_id, payload=body)
        link.sudo().write({
            'data_hash': payload_hash, 'error': False,
            'name': body['name'], 'salla_price': body.get('price', link.salla_price),
        })
        return True

    def _sync_products(self):
        self.ensure_one()
        Map = self.env['toruq.salla.product'].sudo()
        if not self.last_pull_date:
            # Link existing Salla products first so nothing is created twice.
            self._pull_products()
            self.env.cr.commit()
        published = self._published_products()
        links = Map.search([('store_id', '=', self.id), ('product_id', 'in', published.ids)])
        linked_ids = set(links.mapped('product_id').ids)
        created = updated = 0
        errors = []
        no_sku = published.filtered(lambda p: not (p.default_code or '').strip() and p.id not in linked_ids)
        if no_sku:
            errors.append(_('%s published products have no Internal Reference (SKU) and were skipped: %s') % (
                len(no_sku), ', '.join(no_sku[:5].mapped('display_name'))))
        if self.auto_create_products:
            for product in published.filtered(lambda p: p.id not in linked_ids and (p.default_code or '').strip()):
                if created >= CREATE_PER_RUN:
                    break
                try:
                    self._create_in_salla(product)
                    self.env.cr.commit()
                    created += 1
                except SallaFatalError as exc:
                    self.env.cr.rollback()
                    errors.append(str(exc))
                    break
                except UserError as exc:
                    self.env.cr.rollback()
                    _logger.warning('toruq_salla: create %s failed: %s', product.default_code, exc)
                    errors.append(_('Could not create %(sku)s in Salla: %(err)s') % {
                        'sku': product.default_code, 'err': exc})
        for link in links.filtered(lambda l: not l.exclude and not l.missing_in_salla):
            if updated >= UPDATE_PER_RUN:
                break
            try:
                if self._update_in_salla(link):
                    updated += 1
                self.env.cr.commit()
            except SallaFatalError as exc:
                self.env.cr.rollback()
                errors.append(str(exc))
                break
            except UserError as exc:
                self.env.cr.rollback()
                link.sudo().write({'error': str(exc)[:250]})
                self.env.cr.commit()
        if errors:
            self.sudo().write({'last_error': '\n'.join(errors[:5])[:1500]})
            self.env.cr.commit()
        return created, updated

    def action_sync_products(self):
        self._check_manager()
        created = updated = 0
        for store in self:
            c, u = store._sync_products()
            created += c
            updated += u
        message = _('%(c)s products created and %(u)s updated in Salla.') % {'c': created, 'u': updated}
        if any(store.last_error for store in self):
            return self._notify(message + ' ' + _('Some items had problems, see Last Error on the store.'), 'warning')
        return self._notify(message)

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------
    def _get_location_ids(self):
        self.ensure_one()
        store = self.sudo()
        if store.location_ids:
            return store.location_ids.ids
        return store.warehouse_id.lot_stock_id.ids

    def _real_qty_map(self, product_ids, loc_ids):
        res = {}
        if not product_ids or not loc_ids:
            return res
        Quant = self.env['stock.quant'].sudo()
        for i in range(0, len(product_ids), 5000):
            rows = Quant._read_group(
                [('product_id', 'in', product_ids[i:i + 5000]), ('location_id', 'child_of', loc_ids)],
                ['product_id'], ['quantity:sum', 'reserved_quantity:sum'])
            for product, qty, reserved in rows:
                res[product.id] = (qty or 0.0) - (reserved or 0.0)
        return res

    def _draft_qty_map(self, product_ids):
        res = {}
        if not product_ids:
            return res
        rows = self.env['sale.order.line'].sudo()._read_group(
            [('order_id.toruq_salla_store_id', '=', self.id), ('order_id.state', 'in', ('draft', 'sent')),
             ('product_id', 'in', product_ids)],
            ['product_id'], ['product_uom_qty:sum'])
        for product, qty in rows:
            res[product.id] = qty or 0.0
        return res

    def _pending_qty_by_sku(self):
        res = {}
        events = self.env['toruq.salla.event'].sudo().search([
            ('store_id', '=', self.id), ('event', '=', 'order.created'),
            ('state', 'in', ('pending', 'failed'))])
        for ev in events:
            try:
                data = json.loads(ev.payload or '{}').get('data') or {}
            except ValueError:
                continue
            for item in data.get('items') or []:
                sku = (item.get('sku') or '').strip()
                if sku:
                    res[sku] = res.get(sku, 0.0) + _money(item.get('quantity'))
        return res

    def _reserve_for(self, product):
        if not self.safety_enabled:
            return 0
        tmpl = product.product_tmpl_id
        value = tmpl.toruq_salla_reserve if tmpl.toruq_salla_reserve_custom else self.safety_default
        return max(0, value or 0)

    def _targets_for_products(self, products):
        # returns {product_id: (odoo_available, reserve, qty_to_send)}
        self.ensure_one()
        ids = products.ids
        real = self._real_qty_map(ids, self._get_location_ids())
        drafts = self._draft_qty_map(ids)
        pending = self._pending_qty_by_sku()
        res = {}
        for product in products:
            avail = int(math.floor(real.get(product.id, 0.0)))
            reserve = self._reserve_for(product)
            held = drafts.get(product.id, 0.0) + pending.get((product.default_code or '').strip(), 0.0)
            res[product.id] = (avail, reserve, max(0, int(avail - held - reserve)))
        return res

    def _compute_targets(self, links):
        # returns {link_id: (odoo_available, reserve, qty_to_send)}
        self.ensure_one()
        products = links.mapped('product_id')
        by_product = self._targets_for_products(products) if products else {}
        return {link.id: by_product.get(link.product_id.id, (0, 0, 0)) for link in links if link.product_id}

    def _push_stock(self, links, compare='pushed'):
        self.ensure_one()
        eligible = links.filtered(
            lambda l: l.product_id and l.product_id.is_storable and not l.exclude and not l.missing_in_salla
            and l.salla_type in ('', 'product', False))
        if not eligible:
            return 0
        targets = self._compute_targets(eligible)
        now = fields.Datetime.now()
        retry_after = now - timedelta(hours=1)
        changes = []
        for link in eligible:
            target = targets[link.id][2]
            if compare == 'salla':
                differs = link.salla_unlimited or link.salla_qty != target
            elif compare == 'force':
                differs = True
            else:
                differs = link.last_pushed_qty != target
            if not differs:
                continue
            if compare == 'pushed' and link.fail_count >= MAX_FAILS and (
                    link.last_push_attempt and link.last_push_attempt > retry_after):
                continue
            changes.append((link, target))
        done = 0
        for i in range(0, min(len(changes), PUSH_CHUNK * MAX_CHUNKS), PUSH_CHUNK):
            chunk = changes[i:i + PUSH_CHUNK]
            items = []
            for link, target in chunk:
                item = {'identifer_type': 'id', 'identifer': link.salla_product_id,
                        'quantity': target, 'mode': 'overwrite'}
                if self.branch_ref:
                    item['branch'] = self.branch_ref
                items.append(item)
            try:
                self._api('POST', '/products/quantities/bulk', payload={'products': items})
            except UserError as exc:
                for link, _target in chunk:
                    link.sudo().write({'fail_count': link.fail_count + 1, 'error': str(exc)[:250],
                                       'last_push_attempt': now})
                self.sudo().write({'last_error': str(exc)[:500]})
                break
            for link, target in chunk:
                link.sudo().write({'last_pushed_qty': target, 'salla_qty': target, 'salla_unlimited': False,
                                   'last_push_date': now, 'last_push_attempt': now,
                                   'fail_count': 0, 'error': False})
            done += len(chunk)
        if done:
            self.sudo().write({'last_push_date': now})
        return done

    def action_push_stock(self):
        self._check_manager()
        total = 0
        Map = self.env['toruq.salla.product'].sudo()
        for store in self:
            links = Map.search([('store_id', '=', store.id), ('product_id', '!=', False)])
            total += store._push_stock(links, compare='force')
        return self._notify(_('%s products updated in Salla.') % total)

    # ------------------------------------------------------------------
    # Cron entry points
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync(self):
        Map = self.env['toruq.salla.product'].sudo()
        for store in self.search([('state', '=', 'connected')]):
            try:
                self.env['toruq.salla.event']._process_store(store)
                store._sync_products()
                if store.stock_sync_enabled:
                    store._push_stock(Map.search([('store_id', '=', store.id), ('product_id', '!=', False)]))
            except Exception as exc:
                self.env.cr.rollback()
                _logger.exception('toruq_salla: sync failed for store %s', store.id)
                store.sudo().write({'last_error': str(exc)[:500]})
            self.env.cr.commit()

    @api.model
    def _cron_pull(self):
        Map = self.env['toruq.salla.product'].sudo()
        for store in self.search([('state', '=', 'connected')]):
            try:
                store._pull_products()
                if store.stock_sync_enabled:
                    store._push_stock(Map.search([('store_id', '=', store.id), ('product_id', '!=', False)]),
                                      compare='salla')
            except Exception as exc:
                self.env.cr.rollback()
                _logger.exception('toruq_salla: pull failed for store %s', store.id)
                store.sudo().write({'last_error': str(exc)[:500]})
            self.env.cr.commit()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def _fetch_items(self, order_id):
        res = self._api('GET', '/orders/items', params={'order_id': order_id})
        data = res.get('data')
        return data if isinstance(data, list) else []

    def _service_product(self, kind):
        self.ensure_one()
        field = 'shipping_product_id' if kind == 'shipping' else 'fee_product_id'
        product = self[field]
        if not product:
            product = self.env['product.product'].sudo().create({
                'name': _('Salla Shipping') if kind == 'shipping' else _('Salla Cash on Delivery Fee'),
                'type': 'service',
                'sale_ok': True,
                'purchase_ok': False,
                'list_price': 0.0,
            })
            self.sudo().write({field: product.id})
        return product

    def _get_partner(self, cust):
        self.ensure_one()
        Partner = self.env['res.partner'].sudo().with_company(self.company_id)
        cid = str(cust.get('id') or '')
        if cid:
            partner = Partner.search([('toruq_salla_customer_id', '=', cid)], limit=1)
            if partner:
                return partner
        code = str(cust.get('mobile_code') or '').replace(' ', '')
        mobile = str(cust.get('mobile') or '').replace(' ', '')
        phone = ('%s%s' % (code, mobile)) if mobile else ''
        email = (cust.get('email') or '').strip().lower()
        partner = Partner
        if email:
            partner = Partner.search([('email', '=ilike', email)], limit=1)
        if not partner and phone:
            partner = Partner.search([('phone', '=', phone)], limit=1)
        if partner:
            if cid and not partner.toruq_salla_customer_id:
                partner.toruq_salla_customer_id = cid
            return partner
        name = (cust.get('full_name') or ('%s %s' % (cust.get('first_name') or '', cust.get('last_name') or ''))).strip()
        return Partner.create({
            'name': name or _('Salla Customer %s') % (cid or ''),
            'email': email or False,
            'phone': phone or False,
            'city': cust.get('city') or False,
            'toruq_salla_customer_id': cid or False,
            'company_id': False,
        })

    def _import_order(self, payload):
        self.ensure_one()
        SO = self.env['sale.order']
        if self.order_import_mode == 'none':
            return SO
        data = payload.get('data') or {}
        oid = str(data.get('id') or '')
        if not oid:
            raise UserError(_('The Salla order id is missing.'))
        SO = SO.sudo().with_company(self.company_id)
        existing = SO.search([('toruq_salla_store_id', '=', self.id), ('toruq_salla_order_id', '=', oid)], limit=1)
        if existing:
            return existing
        items = data.get('items') or self._fetch_items(oid)
        if not items:
            raise UserError(_('The Salla order has no items.'))
        partner = self._get_partner(data.get('customer') or {})
        reference = str(data.get('reference_id') or oid)
        lines, missing = [], []
        for item in items:
            sku = (item.get('sku') or '').strip()
            product = self._find_product(sku)
            qty = _money(item.get('quantity')) or 1.0
            amounts = item.get('amounts') or {}
            price = _money(amounts.get('price_without_tax')) or _money(item.get('price'))
            if not product:
                if self.unmatched_policy == 'error':
                    missing.append(sku or item.get('name') or '?')
                else:
                    lines.append((0, 0, {'display_type': 'line_note', 'name': '%s x %s (SKU: %s)' % (
                        item.get('name') or '', qty, sku or '-')}))
                continue
            base = price * qty
            discount = _money(amounts.get('total_discount'))
            pct = min(100.0, discount / base * 100.0) if base and discount else 0.0
            lines.append((0, 0, {
                'product_id': product.id,
                'name': item.get('name') or product.display_name,
                'product_uom_qty': qty,
                'price_unit': price,
                'discount': pct,
            }))
        if missing:
            raise UserError(_('Products not found in Odoo (link them by SKU): %s') % ', '.join(missing))
        order_amounts = data.get('amounts') or {}
        shipping = _money(order_amounts.get('shipping_cost'))
        if shipping > 0:
            lines.append((0, 0, {'product_id': self._service_product('shipping').id, 'product_uom_qty': 1.0,
                                 'price_unit': shipping}))
        fee = _money(order_amounts.get('cash_on_delivery'))
        if fee > 0:
            lines.append((0, 0, {'product_id': self._service_product('fee').id, 'product_uom_qty': 1.0,
                                 'price_unit': fee}))
        slug = _status_slug(data)
        method = str(data.get('payment_method') or '').lower()
        vals = {
            'partner_id': partner.id,
            'company_id': self.company_id.id,
            'origin': 'Salla %s' % reference,
            'client_order_ref': reference,
            'toruq_salla_store_id': self.id,
            'toruq_salla_order_id': oid,
            'toruq_salla_reference': reference,
            'toruq_salla_status': slug,
            'toruq_salla_payment_method': method,
            'order_line': lines,
        }
        if self.warehouse_id:
            vals['warehouse_id'] = self.warehouse_id.id
        if self.salesperson_id:
            vals['user_id'] = self.salesperson_id.id
        order = SO.create(vals)
        order.message_post(body=_('Imported from Salla order %(ref)s (status: %(status)s, payment: %(pay)s).') % {
            'ref': reference, 'status': slug or '-', 'pay': method or '-'})
        salla_total = _money(order_amounts.get('total'))
        if salla_total and abs(salla_total - order.amount_total) > 0.05:
            order.message_post(body=_('Total differs: Salla %(salla).2f vs Odoo %(odoo).2f. Please review taxes and discounts.') % {
                'salla': salla_total, 'odoo': order.amount_total})
        if self.order_import_mode == 'confirmed':
            order.action_confirm()
            self._post_actions(order, slug, method)
        return order

    def _find_order(self, data):
        self.ensure_one()
        oid = str(data.get('id') or '')
        if not oid:
            return self.env['sale.order']
        return self.env['sale.order'].sudo().search([
            ('toruq_salla_store_id', '=', self.id), ('toruq_salla_order_id', '=', oid)], limit=1)

    def _update_order(self, payload):
        self.ensure_one()
        data = payload.get('data') or {}
        order = self._find_order(data)
        if not order:
            return False
        slug = _status_slug(data)
        method = str(data.get('payment_method') or order.toruq_salla_payment_method or '').lower()
        vals = {}
        if slug and slug != order.toruq_salla_status:
            vals['toruq_salla_status'] = slug
            order.message_post(body=_('Salla status changed to: %s') % slug)
        if method != order.toruq_salla_payment_method:
            vals['toruq_salla_payment_method'] = method
        if vals:
            order.write(vals)
        if slug in CANCEL_SLUGS:
            self._cancel_order(order)
        else:
            self._post_actions(order, slug or order.toruq_salla_status, method)
        return True

    def _cancel_by_payload(self, payload):
        order = self._find_order(payload.get('data') or {})
        if order:
            self._cancel_order(order)
        return True

    def _note_refund(self, payload):
        order = self._find_order(payload.get('data') or {})
        if order:
            order.message_post(body=_('Salla reported a refund for this order. Please create the credit note manually.'))
        return True

    def _cancel_order(self, order):
        if order.state == 'cancel':
            return
        if any(p.state == 'done' for p in order.picking_ids):
            order.message_post(body=_('Cancelled in Salla but a delivery is already done. Please handle it manually.'))
            return
        self._safe_step(order, _('cancel order'), lambda: order.with_context(disable_cancel_warning=True).action_cancel())

    def _safe_step(self, order, label, func):
        try:
            with self.env.cr.savepoint():
                func()
        except Exception as exc:
            _logger.warning('toruq_salla: step %s failed on %s: %s', label, order.name, exc)
            order.message_post(body=_('Automatic step failed (%(step)s): %(err)s') % {'step': label, 'err': exc})

    def _post_actions(self, order, slug, method):
        self.ensure_one()
        if order.state not in ('sale', 'done'):
            return
        slug = (slug or '').lower()
        if self.auto_invoice and slug in _csv(self.invoice_statuses):
            invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state != 'cancel')[:1]
            if not invoice:
                def make_invoice():
                    inv = order._create_invoices()
                    inv.action_post()
                self._safe_step(order, _('create invoice'), make_invoice)
                invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice' and m.state == 'posted')[:1]
            paid_now = method != 'cod' or slug in ('delivered', 'completed')
            if (self.auto_payment and self.payment_journal_id and invoice and invoice.state == 'posted'
                    and invoice.payment_state not in ('paid', 'in_payment') and paid_now):
                def pay():
                    self.env['account.payment.register'].sudo().with_context(
                        active_model='account.move', active_ids=invoice.ids,
                    ).create({'journal_id': self.payment_journal_id.id})._create_payments()
                self._safe_step(order, _('register payment'), pay)
        if self.auto_deliver and slug in _csv(self.deliver_statuses):
            for picking in order.picking_ids.filtered(lambda p: p.state == 'assigned' and p.picking_type_code == 'outgoing'):
                self._safe_step(order, _('validate delivery'), lambda pk=picking: pk.with_context(
                    skip_backorder=True, skip_sms=True).button_validate())

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    @api.model
    def get_dashboard_data(self, store_id=None):
        stores = self.search([])
        if not stores:
            return {'stores': [], 'store': False}
        store = (stores.filtered(lambda s: s.id == store_id) or stores)[:1]
        s = store.sudo()
        Map = self.env['toruq.salla.product'].sudo()
        base = [('store_id', '=', s.id)]
        live = base + [('missing_in_salla', '=', False)]

        def count(domain):
            return Map.search_count(domain)

        links = Map.search(live + [('product_id', '!=', False)], limit=5000)
        targets = s._compute_targets(links)
        mismatch = sum(1 for l in links if l.product_id.is_storable and not l.exclude
                       and (l.salla_unlimited or l.salla_qty != targets[l.id][2]))
        SO = self.env['sale.order'].sudo()
        so_dom = [('toruq_salla_store_id', '=', s.id)]
        today = fields.Date.context_today(self)
        week_ago = today - timedelta(days=6)
        rows = SO._read_group(so_dom + [('date_order', '>=', fields.Datetime.to_string(datetime.combine(week_ago, datetime.min.time())))],
                              ['date_order:day'], ['__count', 'amount_total:sum'])
        by_day = {}
        for day, cnt, total in rows:
            by_day[fields.Date.to_string(day.date() if isinstance(day, datetime) else day)] = (cnt, total or 0.0)
        series = []
        for i in range(7):
            d = fields.Date.to_string(week_ago + timedelta(days=i))
            series.append({'date': d, 'count': by_day.get(d, (0, 0.0))[0], 'total': round(by_day.get(d, (0, 0.0))[1], 2)})
        sums = SO._read_group(so_dom, [], ['amount_total:sum'])
        Event = self.env['toruq.salla.event'].sudo()
        low = Map.search(live + [('salla_unlimited', '=', False), ('salla_qty', '<=', 5)], order='salla_qty asc', limit=8)
        failed = Event.search([('store_id', '=', s.id), ('state', '=', 'failed')], order='id desc', limit=5)
        published = self.env['product.template'].sudo().search_count([('toruq_salla_publish', '=', True)])
        return {
            'stores': [{'id': x.id, 'name': x.name} for x in stores],
            'store': {
                'id': s.id, 'name': s.name, 'state': s.state, 'store_name': s.store_name or '',
                'merchant_id': s.merchant_id or '', 'last_pull': fields.Datetime.to_string(s.last_pull_date) if s.last_pull_date else '',
                'last_push': fields.Datetime.to_string(s.last_push_date) if s.last_push_date else '',
                'token_expires': fields.Datetime.to_string(s.token_expires) if s.token_expires else '',
                'order_mode': s.order_import_mode, 'stock_sync': s.stock_sync_enabled, 'safety': s.safety_enabled,
                'reserve': s.safety_default, 'warehouse': s.warehouse_id.name or '', 'last_error': s.last_error or '',
                'currency': s.company_id.currency_id.symbol or '',
                'can_manage': self.env.user.has_group('toruq_salla.group_manager'),
            },
            'kpi': {
                'products': count(live), 'linked': count(live + [('link_state', '=', 'linked')]),
                'unmatched': count(live + [('link_state', '=', 'unmatched')]), 'missing': count(base + [('missing_in_salla', '=', True)]),
                'on_sale': count(live + [('salla_status', '=', 'sale')]), 'hidden': count(live + [('salla_status', '=', 'hidden')]),
                'out': count(live + [('salla_status', '=', 'out')]),
                'stock_total': sum(Map.search(live + [('salla_unlimited', '=', False)]).mapped('salla_qty')),
                'out_of_stock': count(live + [('salla_unlimited', '=', False), ('salla_qty', '<=', 0)]),
                'low_stock': count(live + [('salla_unlimited', '=', False), ('salla_qty', '>', 0), ('salla_qty', '<=', 5)]),
                'mismatch': mismatch, 'published': published,
                'orders_total': SO.search_count(so_dom), 'orders_week': sum(x['count'] for x in series),
                'sales_total': round(sums[0][0] or 0.0, 2) if sums else 0.0,
                'events_pending': Event.search_count([('store_id', '=', s.id), ('state', '=', 'pending')]),
                'events_failed': Event.search_count([('store_id', '=', s.id), ('state', '=', 'failed')]),
            },
            'series': series,
            'low_products': [{'name': l.name or l.sku or '', 'sku': l.sku or '', 'salla_qty': l.salla_qty,
                              'odoo_qty': targets.get(l.id, (0, 0, 0))[0] if l.product_id else None} for l in low],
            'failed_events': [{'id': e.id, 'event': e.event, 'error': (e.error or '')[:160],
                               'date': fields.Datetime.to_string(e.received_at)} for e in failed],
        }
