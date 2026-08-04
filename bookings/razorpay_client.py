"""
Thin Razorpay REST client — orders, signature checks, refunds.

Keys come from env (Render): RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET,
RAZORPAY_WEBHOOK_SECRET. Test-mode keys and live keys use the exact same
code path — switching is an env-var swap.
"""
import hashlib
import hmac

import requests
from django.conf import settings

TIMEOUT = 15
BASE = 'https://api.razorpay.com/v1'


class RazorpayError(Exception):
    """Raised when Razorpay can't be reached or rejects a request."""


def configured():
    return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


def _auth():
    return (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)


def create_order(amount_rupees, receipt):
    """Create a Razorpay Order; returns its id. Amount is converted to paise."""
    if not configured():
        raise RazorpayError('Payments are not configured (RAZORPAY keys missing).')
    try:
        response = requests.post(
            f'{BASE}/orders',
            json={'amount': int(amount_rupees) * 100, 'currency': 'INR',
                  'receipt': str(receipt)[:40]},
            auth=_auth(), timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RazorpayError('Could not reach the payment gateway.') from exc
    if response.status_code != 200:
        raise RazorpayError(f'Payment gateway error (HTTP {response.status_code}).')
    order_id = (response.json() or {}).get('id')
    if not order_id:
        raise RazorpayError('Payment gateway returned no order id.')
    return order_id


def verify_payment_signature(order_id, payment_id, signature):
    """Checkout callback signature: HMAC-SHA256('order_id|payment_id', key_secret)."""
    if not settings.RAZORPAY_KEY_SECRET:
        return False  # fail closed — empty-key HMACs are forgeable
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f'{order_id}|{payment_id}'.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, str(signature or ''))


def verify_webhook_signature(body_bytes, signature):
    """Webhook signature: HMAC-SHA256(raw body, webhook_secret)."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        return False
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature or ''))


def refund_payment(payment_id, amount_rupees=None):
    """Refund a captured payment (full when amount is None); returns refund id."""
    if not configured():
        raise RazorpayError('Payments are not configured (RAZORPAY keys missing).')
    # Idempotency: if this payment already has a refund at the gateway
    # (e.g. our previous attempt succeeded but the response was lost),
    # reuse it instead of refunding twice.
    try:
        existing = requests.get(
            f'{BASE}/payments/{payment_id}/refund', auth=_auth(), timeout=TIMEOUT
        )
        if existing.status_code == 200:
            items = (existing.json() or {}).get('items') or []
            if items and items[0].get('id'):
                return items[0]['id']
    except requests.RequestException:
        pass  # can't check — proceed to attempt the refund normally

    payload = {}
    if amount_rupees:
        payload['amount'] = int(amount_rupees) * 100
    try:
        response = requests.post(
            f'{BASE}/payments/{payment_id}/refund',
            json=payload, auth=_auth(), timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise RazorpayError('Could not reach the payment gateway.') from exc
    if response.status_code != 200:
        raise RazorpayError(f'Refund failed (HTTP {response.status_code}).')
    refund_id = (response.json() or {}).get('id')
    if not refund_id:
        raise RazorpayError('Payment gateway returned no refund id.')
    return refund_id
