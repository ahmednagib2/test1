from odoo import _, api, fields, models
from odoo.exceptions import UserError
import json
import logging

_logger = logging.getLogger(__name__)

# Product synchronization patch: the current token was issued without
# products.read_write. The complete existing model must retain all original
# methods; this marker is intentionally replaced by the deployed patch.
# The sync must not create a nested savepoint after an HTTP/API failure.

PRODUCTS_SCOPE_ERROR = _(
    'Salla refused product access. Re-authorize the store application with '
    'the products.read_write scope, then test the connection again.'
)


def _product_scope_error(message):
    text = str(message or '')
    if 'products.read_write' in text or 'access token' in text.lower():
        return UserError(PRODUCTS_SCOPE_ERROR)
    return UserError(text or _('Salla product synchronization failed.'))
