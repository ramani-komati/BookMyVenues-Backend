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
import logging
import secrets

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# How long we wait for the provider to answer before giving up (seconds).
SMS_TIMEOUT = 10

FAST2SMS_URL = 'https://www.fast2sms.com/dev/bulkV2'


class OTPSendError(Exception):
    """Raised when the SMS could not be sent."""


def generate_code() -> str:
    """A cryptographically random 6-digit code, e.g. '048392'.

    secrets (not random) — designed for security-sensitive values."""
    return f'{secrets.randbelow(1_000_000):06d}'


def _fast2sms_params(phone: str, code: str) -> dict:
    """
    Fast2SMS route parameters.

    'q' (quick) is the default: it works on any recharged account and we
    compose the message ourselves. 'otp' is Fast2SMS's dedicated OTP route —
    cleaner, but it requires website verification on their dashboard. Switch
    with FAST2SMS_ROUTE once that verification is done; no code change.
    """
    if settings.FAST2SMS_ROUTE == 'dlt':
        # The approved template supplies the wording; we only fill {#var#}.
        # Approved text:
        #   "Your OTP for THEBOOKMY VENUES is {#var#}. This OTP is valid for
        #    10 minutes. Do not share this OTP with anyone."
        return {
            'route': 'dlt',
            'sender_id': settings.FAST2SMS_SENDER_ID,
            'message': settings.FAST2SMS_DLT_TEMPLATE_ID,
            'variables_values': code,
            'flash': '0',
            'numbers': phone,
        }
    if settings.FAST2SMS_ROUTE == 'otp':
        return {'route': 'otp', 'variables_values': code, 'numbers': phone}
    return {
        'route': 'q',
        'message': (
            f'{code} is your BookMyVenues verification code. '
            'It expires in 5 minutes. Do not share it with anyone.'
        ),
        'language': 'english',
        'flash': '0',
        'numbers': phone,
    }


def _send_via_fast2sms(phone: str, code: str) -> None:
    """Send the code as a real SMS through Fast2SMS."""
    try:
        response = requests.get(
            FAST2SMS_URL,
            params=_fast2sms_params(phone, code),
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


RESEND_URL = 'https://api.resend.com/emails'


def send_otp_email(email: str, code: str) -> None:
    """
    Deliver the SAME code by email via Resend.

    Resend does not generate anything — like the SMS provider, it is only a
    delivery pipe for the code WE generated. Pass the same `code` you passed
    to send_otp_sms and both messages carry the same number.

    Raises OTPSendError on failure; callers decide whether that is fatal
    (see deliver_otp — it is not, as long as one channel got through).
    """
    if not settings.RESEND_API_KEY:
        raise OTPSendError('Email service is not configured (RESEND_API_KEY unset).')
    if not email:
        raise OTPSendError('No email address to send to.')

    from BookMyVenue.emails import render_otp_email
    from accounts.models import PhoneOTP

    subject, html, text = render_otp_email(code, PhoneOTP.LIFETIME_MINUTES)
    try:
        response = requests.post(
            RESEND_URL,
            headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}'},
            json={
                'from': settings.RESEND_FROM,
                'to': [email],
                'subject': subject,
                # Both parts: some clients show text, and multipart mail is
                # treated better by spam filters than HTML alone.
                'html': html,
                'text': text,
            },
            timeout=SMS_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise OTPSendError('Could not reach the email service.') from exc

    if response.status_code not in (200, 201):
        # The body can echo the code back — log only the status.
        raise OTPSendError(f'Email service returned HTTP {response.status_code}.')


def deliver_otp(code: str, phone: str, email: str = '') -> None:
    """
    Send ONE code over every channel we can.

    The code is generated once by the caller and stored once (hashed), so it
    does not matter which message the user reads it from — verification checks
    the single stored copy.

    Succeeds if AT LEAST ONE channel delivered. Only when every channel fails
    do we raise, because storing an OTP the user never received would lock
    them out. SECURITY: the code is never logged, on any path.
    """
    delivered = False
    errors = []

    # Each channel is isolated behind a BROAD except, not just OTPSendError.
    # A channel can fail in ways that are not OTPSendError — a missing
    # template, a bad sender address, any library raising something new — and
    # if that escaped, one broken channel would take login down even though
    # the other had already delivered the code. Worse, the caller stores the
    # OTP only after this returns, so an escape means the user receives a
    # code the server never saved.
    try:
        send_otp_sms(phone, code)
        delivered = True
    except Exception as exc:
        errors.append(f'sms: {exc}')
        logger.warning('OTP SMS delivery failed: %s', exc)

    if email and settings.RESEND_API_KEY:
        try:
            send_otp_email(email, code)
            delivered = True
        except Exception as exc:
            errors.append(f'email: {exc}')
            logger.warning('OTP email delivery failed: %s', exc)

    if not delivered:
        raise OTPSendError('; '.join(errors) or 'No delivery channel configured.')
