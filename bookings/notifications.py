"""
Booking notifications — sent when a booking is confirmed.

Two channels, same content:
  * email  (Resend)  — effectively free, so it is the default
  * SMS    (Fast2SMS) — OFF by default; see NOTIFY_SMS_ENABLED below

WHY SMS IS OFF BY DEFAULT
-------------------------
Fast2SMS's 'q' (quick) route costs several rupees per message. A booking sends
two (customer confirm + vendor alert), which together cost more than the
platform fee earns on that booking — every booking would lose money.
The cheap 'otp' route cannot carry these messages: it only sends a bare code.
Real transactional SMS in India needs DLT-registered templates.

So: email carries the notifications, SMS stays for OTP. Flip NOTIFY_SMS_ENABLED
to True once DLT templates are approved and the per-message price is sane.

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


def _send(channel, target, body, subject=''):
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
            _send_email(target, subject, body)
        else:
            if not settings.NOTIFY_SMS_ENABLED:
                return False
            send_otp_sms(target, body)
        return True
    except Exception as exc:
        logger.warning('Notification via %s failed: %s', channel, exc)
        return False


def _send_email(address, subject, body):
    """Reuses the Resend transport; the subject differs from the OTP mail."""
    import requests

    if not settings.RESEND_API_KEY:
        raise OTPSendError('RESEND_API_KEY unset.')
    response = requests.post(
        'https://api.resend.com/emails',
        headers={'Authorization': f'Bearer {settings.RESEND_API_KEY}'},
        json={
            'from': settings.RESEND_FROM,
            'to': [address],
            'subject': subject,
            'text': body,
        },
        timeout=10,
    )
    if response.status_code not in (200, 201):
        raise OTPSendError(f'Resend returned HTTP {response.status_code}.')


def notify_booking_confirmed(booking):
    """Tell the CUSTOMER it is booked and the VENDOR that they have a booking.

    Best-effort: returns True if at least one message got through, so the
    caller can record it and a later catch-up run can retry the rest.
    """
    when = f'{booking.date:%d %b %Y} at {_slot_text(booking)}'
    sent = False

    # --- customer ---
    customer_email = getattr(booking.user, 'email', '') or ''
    body = (
        f'Your booking is confirmed.\n\n'
        f'Venue: {booking.venue_name}\n'
        f'When: {when}\n'
        f'Amount: {_rupees(booking.amount)}\n'
        f'Booking ID: {booking.id}\n\n'
        f'Show this booking ID at the venue.'
    )
    sent |= _send('email', customer_email, body,
                  subject=f'Booking confirmed — {booking.venue_name}')
    sent |= _send('sms', booking.phone,
                  f'Booking confirmed: {booking.venue_name}, {when}. '
                  f'ID {booking.id}')

    # --- vendor ---
    vendor = getattr(booking.listing, 'vendor', None)
    if vendor is not None:
        who = booking.customer_name or 'A customer'
        contact = booking.phone or 'no phone on file'
        vendor_body = (
            f'You have a new booking.\n\n'
            f'Venue: {booking.venue_name}\n'
            f'When: {when}\n'
            f'Customer: {who} ({contact})\n'
            f'Amount: {_rupees(booking.amount)}\n'
            f'Booking ID: {booking.id}\n'
        )
        sent |= _send('email', vendor.email or '', vendor_body,
                      subject=f'New booking — {booking.venue_name}')
        sent |= _send('sms', vendor.phone,
                      f'New booking: {booking.venue_name}, {when}, '
                      f'{who}. ID {booking.id}')
    return sent
