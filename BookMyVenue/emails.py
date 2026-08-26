"""
Transactional email rendering.

Every email goes out as HTML *and* plain text. The text part is not decoration:
some clients render it, some readers prefer it, and spam filters treat a
multipart message better than an HTML-only one.

Templates live in templates/emails/ so they can be edited without touching
Python. See templates/emails/_base.html for why the markup is table-based.
"""
from django.conf import settings
from django.template.loader import render_to_string
from urllib.parse import urlparse


def _shell_context():
    site = settings.BRAND_SITE_URL
    return {
        'logo_url': settings.BRAND_LOGO_URL,
        'site_url': site,
        'site_domain': urlparse(site).netloc or site,
    }


def render_otp_email(code, minutes):
    """(subject, html, text) for the sign-in code."""
    html = render_to_string('emails/otp.html', {
        **_shell_context(), 'code': code, 'minutes': minutes,
        'eyebrow': 'Sign in',
    })
    text = (
        f'Your TheBookMyVenues verification code is {code}.\n\n'
        f'It expires in {minutes} minutes. If you did not request it, '
        f'you can ignore this email.\n\n'
        f'Never share this code with anyone.'
    )
    return f'{code} is your TheBookMyVenues code', html, text


def render_booking_email(*, venue, when, amount, booking_id,
                         customer=None, phone=None, is_vendor=False):
    """(subject, html, text) for a confirmed booking.

    One template serves both sides — the vendor's copy adds the customer's
    contact details and reads as an alert rather than a receipt.
    """
    rows = [('Venue', venue), ('When', when)]
    if is_vendor and customer:
        rows.append(('Customer', f'{customer} · {phone}' if phone else customer))
    rows.append(('Booking ID', booking_id))

    html = render_to_string('emails/booking_confirmed.html', {
        **_shell_context(), 'venue': venue, 'rows': rows,
        'amount': f'{int(amount):,}', 'is_vendor': is_vendor,
        'eyebrow': 'New booking' if is_vendor else 'Booking confirmed',
    })

    lines = [
        'You have a new booking.' if is_vendor else 'Your booking is confirmed.',
        '', f'Venue: {venue}', f'When: {when}',
    ]
    if is_vendor and customer:
        lines.append(f'Customer: {customer}' + (f' ({phone})' if phone else ''))
    lines += [f'Amount: Rs {int(amount):,}', f'Booking ID: {booking_id}']
    if not is_vendor:
        lines += ['', 'Show this booking ID at the venue.']

    subject = (f'New booking — {venue}' if is_vendor
               else f'Booking confirmed — {venue}')
    return subject, html, '\n'.join(lines)
