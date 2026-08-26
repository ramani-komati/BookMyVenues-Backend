"""
Booking notifications — sent when a booking is confirmed.

Two channels, same content:
  * email (Resend)    — no template registration needed, body sent inline
  * SMS   (Fast2SMS)  — needs a DLT-approved template for the cheap route

Both are on by default (NOTIFY_EMAIL_ENABLED / NOTIFY_SMS_ENABLED). A booking
sends two messages per channel: one to the customer, one to the vendor.

NOTE ON THE SMS ROUTE: the 'q' (quick) route sends arbitrary text and needs no
template, but is priced per message at the higher rate. The ~20 paise rate
comes from a DLT-registered transactional template, which requires the message
text to be registered in advance with matching {#var#} placeholders — see
FRONTEND_API_GUIDE / README for the exact strings to register.

NOTHING HERE MAY BREAK A BOOKING. Every send is best-effort: failures are
logged and swallowed, never raised into the request that created the booking.
A send that fails is simply lost — there is no scheduler to retry it.
"""
import logging

from django.conf import settings

from accounts.otp import OTPSendError, send_otp_email, send_otp_sms



logger = logging.getLogger(__name__)

def _rupees(amount):
    return f'₹{int(amount):,}'


def _slot_text(booking):
    return booking.slots[0] if booking.slots else ''


def _send(channel, target, body, subject='', html=None):
    """One best-effort delivery. Returns True when it got through.

    Catches everything: a notification must never break the booking that
    triggered it.
    """
    if not target:
        return False
    try:
        if channel == 'email':
            if not settings.NOTIFY_EMAIL_ENABLED:
                return False
            _send_email(target, subject, body, html)
        else:
            if not settings.NOTIFY_SMS_ENABLED:
                return False
            send_otp_sms(target, body)
        return True
    except Exception as exc:
        logger.warning('Notification via %s failed: %s', channel, exc)
        return False


def _send_email(address, subject, body, html=None):
    """Resend transport. `html` is optional so callers can send text-only."""
    import requests

    if not settings.RESEND_API_KEY:
        raise OTPSendError('RESEND_API_KEY unset.')
    payload = {
        'from': settings.RESEND_FROM,
        'to': [address],
        'subject': subject,
        'text': body,
    }
    if html:
        payload['html'] = html
    response = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}'},
        json=payload,
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise OTPSendError(f'Resend returned HTTP {response.status_code}.')


def notify_booking_confirmed(booking):
    """Tell the CUSTOMER it is booked and the VENDOR that they have a booking.

    Best-effort: returns True if at least one message got through, so the
    caller can record it and a later catch-up run can retry the rest.
    """
    from BookMyVenue.emails import render_booking_email

    when = f'{booking.date:%d %b %Y} at {_slot_text(booking)}'
    sent = False

    # --- customer ---
    subject, html, text = render_booking_email(
        venue=booking.venue_name, when=when, amount=booking.amount,
        booking_id=booking.id,
    )
    sent |= _send('email', getattr(booking.user, 'email', '') or '', text,
                  subject=subject, html=html)
    sent |= _send('sms', booking.phone,
                  f'Booking confirmed: {booking.venue_name}, {when}. '
                  f'ID {booking.id}')

    # --- vendor ---
    vendor = getattr(booking.listing, 'vendor', None)
    if vendor is not None:
        who = booking.customer_name or 'A customer'
        subject, html, text = render_booking_email(
            venue=booking.venue_name, when=when, amount=booking.amount,
            booking_id=booking.id, customer=who, phone=booking.phone,
            is_vendor=True,
        )
        sent |= _send('email', vendor.email or '', text,
                      subject=subject, html=html)
        sent |= _send('sms', vendor.phone,
                      f'New booking: {booking.venue_name}, {when}, '
                      f'{who}. ID {booking.id}')
    return sent
