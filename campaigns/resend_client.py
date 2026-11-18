"""
Resend transport for PROMOTIONAL mail.

Reuses the existing configuration — settings.RESEND_API_KEY, the same key the
OTP and booking emails use — and never exposes it: it is read here, server
side, and sent only in the Authorization header to api.resend.com.

Why the batch endpoint and not Resend Broadcasts: Broadcasts need every
customer copied into a Resend Audience and consent managed over there. Our
consent, segments and unsubscribe records stay in our own database instead,
and each customer gets their own unsubscribe link, which the batch endpoint
supports (up to 100 individually rendered emails per call).

Errors are classified for the sender (campaigns/sender.py):
  TransientError   worth retrying — network, timeout, 429, 5xx, a concurrent
                   request with the same idempotency key
  PermanentError   the request itself is wrong (4xx) — retrying it unchanged
                   can never succeed
"""
import re

import requests
from django.conf import settings

API = 'https://api.resend.com'
BATCH_LIMIT = 100           # Resend's maximum emails per batch call
TIMEOUT = 30                # seconds


class SendError(Exception):
    pass


class TransientError(SendError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(SendError):
    pass


_EMAIL = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')


def _safe(text):
    """An error message fit to store and show: addresses masked, trimmed."""
    return _EMAIL.sub('<email>', str(text or ''))[:280]


def _headers(idempotency_key):
    headers = {'Authorization': f'Bearer {settings.RESEND_API_KEY}'}
    if idempotency_key:
        # Resend remembers a key for 24 hours: the same request sent again
        # (a retry after a timeout, a worker that crashed mid-call) returns the
        # original result instead of emailing everyone a second time.
        headers['Idempotency-Key'] = idempotency_key[:256]
    return headers


def _post(path, payload, idempotency_key):
    if not settings.RESEND_API_KEY:
        raise PermanentError('Email service is not configured (RESEND_API_KEY unset).')
    try:
        response = requests.post(f'{API}{path}', json=payload,
                                 headers=_headers(idempotency_key), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise TransientError(f'Could not reach Resend ({exc.__class__.__name__}).') from exc

    if response.status_code in (200, 201):
        try:
            return response.json()
        except ValueError:
            return {}

    try:
        body = response.json()
    except ValueError:
        body = {}
    name = str(body.get('name') or '')
    message = _safe(body.get('message') or f'Resend returned HTTP {response.status_code}.')
    if response.status_code == 429 or response.status_code >= 500 or name == 'concurrent_idempotent_requests':
        retry_after = response.headers.get('retry-after')
        try:
            retry_after = float(retry_after) if retry_after else None
        except ValueError:
            retry_after = None
        raise TransientError(f'HTTP {response.status_code}: {message}', retry_after)
    raise PermanentError(f'HTTP {response.status_code}: {message}')


def send_batch(payloads, idempotency_key):
    """Send up to 100 emails in one call -> [resend email id, ...] in order."""
    if not payloads:
        return []
    if len(payloads) > BATCH_LIMIT:
        raise ValueError(f'At most {BATCH_LIMIT} emails per batch.')
    body = _post('/emails/batch', payloads, idempotency_key)
    data = body.get('data') if isinstance(body, dict) else None
    ids = [str((item or {}).get('id') or '') for item in (data or [])]
    # Resend answers index-aligned; pad defensively so a short answer never
    # shifts one customer's id onto another.
    return (ids + [''] * len(payloads))[:len(payloads)]


def send_one(payload, idempotency_key):
    """Send a single email -> its resend id."""
    body = _post('/emails', payload, idempotency_key)
    return str((body or {}).get('id') or '')
