"""
OTP generation and SMS delivery.

Generation & validation happen on OUR server (see models.PhoneOTP and
accounts/views.py). 2Factor.in is used ONLY to deliver the SMS.

SECURITY: the OTP code is never logged or printed anywhere.
"""
import secrets

import requests
from django.conf import settings

# How long we wait for 2Factor to answer before giving up (seconds).
SMS_TIMEOUT = 10


class OTPSendError(Exception):
    """Raised when the SMS could not be sent."""


def generate_code() -> str:
    """A cryptographically random 6-digit code, e.g. '048392'.

    secrets (not random) — designed for security-sensitive values."""
    return f'{secrets.randbelow(1_000_000):06d}'


def send_otp_sms(phone: str, code: str) -> None:
    """
    Deliver the code to the phone via 2Factor.in.

    Raises OTPSendError on any failure so the view can return a clear
    error instead of silently pretending the SMS was sent.
    """
    api_key = settings.TWOFACTOR_API_KEY
    if not api_key:
        raise OTPSendError('SMS service is not configured (TWOFACTOR_API_KEY missing).')

    # The template name at the end forces SMS delivery — without it,
    # some 2Factor accounts fall back to a voice call.
    template = settings.TWOFACTOR_SMS_TEMPLATE
    url = f'https://2factor.in/API/V1/{api_key}/SMS/{phone}/{code}/{template}'
    try:
        response = requests.get(url, timeout=SMS_TIMEOUT)
    except requests.RequestException as exc:
        raise OTPSendError('Could not reach the SMS service.') from exc

    if response.status_code != 200:
        raise OTPSendError(f'SMS service returned HTTP {response.status_code}.')

    payload = response.json()
    if payload.get('Status') != 'Success':
        raise OTPSendError(f"SMS service error: {payload.get('Details', 'unknown')}")
