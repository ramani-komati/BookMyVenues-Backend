"""
OTP generation and SMS delivery.

Generation & validation happen on OUR server (see models.PhoneOTP and
accounts/views.py). The SMS provider ONLY delivers the message.

Two providers are supported and chosen by which key is configured:
  1. Fast2SMS   (FAST2SMS_API_KEY)  — preferred; real SMS
  2. 2Factor.in (TWOFACTOR_API_KEY) — fallback; its SMS route was never
     DLT-approved on our account, so it silently delivers voice calls
Switching provider is therefore a pure environment change.

SECURITY: the OTP code is never logged or printed anywhere.
"""
import secrets

import requests
from django.conf import settings

# How long we wait for the provider to answer before giving up (seconds).
SMS_TIMEOUT = 10

FAST2SMS_URL = 'https://www.fast2sms.com/dev/bulkV2'


class OTPSendError(Exception):
    """Raised when the SMS could not be sent."""


def generate_code() -> str:
    """A cryptographically random 6-digit code, e.g. '048392'.

    secrets (not random) — designed for security-sensitive values."""
    return f'{secrets.randbelow(1_000_000):06d}'


def _send_via_fast2sms(phone: str, code: str) -> None:
    """Fast2SMS OTP route — delivers 'Your OTP: <code>' as a real SMS."""
    try:
        response = requests.get(
            FAST2SMS_URL,
            params={
                'route': 'otp',
                'variables_values': code,
                'numbers': phone,
            },
            headers={'authorization': settings.FAST2SMS_API_KEY},
            timeout=SMS_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OTPSendError('Could not reach the SMS service.') from exc

    if response.status_code != 200:
        raise OTPSendError(f'SMS service returned HTTP {response.status_code}.')

    try:
        payload = response.json()
    except ValueError:
        raise OTPSendError('SMS service returned an unreadable response.')

    # Fast2SMS answers {"return": true, ...} on success.
    if payload.get('return') is not True:
        message = payload.get('message') or 'unknown error'
        if isinstance(message, list):
            message = '; '.join(str(part) for part in message)
        raise OTPSendError(f'SMS service error: {message}')


def _send_via_2factor(phone: str, code: str) -> None:
    """2Factor.in fallback. The template name forces the SMS route —
    without it (and without DLT approval) the account falls back to voice."""
    template = settings.TWOFACTOR_SMS_TEMPLATE
    url = (
        f'https://2factor.in/API/V1/{settings.TWOFACTOR_API_KEY}'
        f'/SMS/{phone}/{code}/{template}'
    )
    try:
        response = requests.get(url, timeout=SMS_TIMEOUT)
    except requests.RequestException as exc:
        raise OTPSendError('Could not reach the SMS service.') from exc

    if response.status_code != 200:
        raise OTPSendError(f'SMS service returned HTTP {response.status_code}.')

    payload = response.json()
    if payload.get('Status') != 'Success':
        raise OTPSendError(f"SMS service error: {payload.get('Details', 'unknown')}")


def send_otp_sms(phone: str, code: str) -> None:
    """
    Deliver the code to the phone, preferring Fast2SMS when configured.

    Raises OTPSendError on any failure so the view can return a clear
    error instead of silently pretending the SMS was sent.
    """
    if settings.FAST2SMS_API_KEY:
        return _send_via_fast2sms(phone, code)
    if settings.TWOFACTOR_API_KEY:
        return _send_via_2factor(phone, code)
    raise OTPSendError('SMS service is not configured (no provider API key set).')
