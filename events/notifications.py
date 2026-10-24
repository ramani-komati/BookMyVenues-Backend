"""
Event ticket notifications — sent when an order is paid.

Reuses bookings/notifications.py's transport wholesale (Resend email, and
Fast2SMS only when NOTIFY_SMS_ENABLED, which stays off until booking SMS
templates are DLT-approved). Nothing here may break a paid order: every send
is best-effort, failures are logged and swallowed.
"""
import logging

from bookings.notifications import _send

logger = logging.getLogger(__name__)


def _when(order):
    times = ''
    if order.start_time and order.end_time:
        times = f' · {order.start_time:%H:%M}–{order.end_time:%H:%M}'
    return f'{order.date:%d %b %Y}{times}'


def _ticket_lines(order):
    return ', '.join(
        f'{item.get("qty")} × {item.get("label")}'
        for item in (order.items or [])
        if item.get('qty')
    )


def _venue_text(order):
    """The full place, one part per line, without repeats: the venue name,
    street address, locality and city from the EVENT when it still exists,
    else the location string frozen on the order at purchase."""
    event = order.event
    parts = []
    if event is not None:
        parts = [event.venue_name, event.address, event.locality, event.city]
    seen, lines = set(), []
    for part in parts:
        for segment in str(part or '').split(','):
            segment = segment.strip()
            if segment and segment.lower() not in seen:
                seen.add(segment.lower())
                lines.append(segment)
    return ', '.join(lines) or order.location


def _ticket_rows(order):
    """'2 × Adult @ ₹500 = ₹1,000' — one line per ticket type bought."""
    lines = []
    for item in order.items or []:
        if not item.get('qty'):
            continue
        price = int(item.get('price') or 0)
        subtotal = int(item.get('subtotal') or price * int(item['qty']))
        lines.append(
            f'{item["qty"]} × {item.get("label")} @ ₹{price:,} = ₹{subtotal:,}'
        )
    return '\n'.join(lines)


def notify_tickets_confirmed(order):
    """Tell the CUSTOMER their tickets are booked and the VENDOR they sold
    some. Only ever called for a VERIFIED payment (events/payments.
    _notify_confirmed), which also guarantees it runs once per order.
    Returns True when at least one message got through."""
    from BookMyVenue.emails import render_tickets_email

    when = _when(order)
    tickets = _ticket_lines(order)
    sent = False

    rows = [
        ('Date & time', when),
        ('Venue', _venue_text(order)),
        ('Tickets', _ticket_rows(order) or tickets),
        ('Subtotal', f'₹{order.subtotal:,}'),
        ('Booking fee', f'₹{order.fee:,}'),
        ('Booking ID', order.id),
    ]
    maps_link = getattr(order.event, 'maps_link', '') or ''
    subject, html, text = render_tickets_email(
        event_name=order.event_name, rows=rows, amount=order.amount,
        maps_link=maps_link,
    )
    sent |= _send(
        'email', getattr(order.user, 'email', '') or '', text,
        subject=subject, html=html,
    )
    sent |= _send(
        'sms', order.phone,
        f'Tickets confirmed: {order.event_name}, {when}. '
        f'{order.ticket_count} ticket(s). Booking ID {order.id}',
    )

    # Who runs the door on the night, and therefore wants to know a ticket
    # just sold. Events are admin-created now and carry NO vendor, so fall
    # back to the owner of the venue the event is HOSTED at — that is the
    # person who has to let the ticket-holder in. A legacy vendor-created
    # event still notifies its own vendor first.
    event = order.event
    vendor = getattr(event, 'vendor', None)
    if vendor is None and getattr(event, 'listing_id', None):
        vendor = event.listing.vendor
    if vendor is not None:
        who = order.customer_name or 'A customer'
        vendor_text = (
            f'{who} booked tickets for {order.event_name}.\n\n'
            f'{when}\nTickets: {tickets}\nAmount: ₹{order.amount:,}\n'
            f'Phone: {order.phone}\nBooking ID: {order.id}'
        )
        sent |= _send(
            'email', vendor.email or '', vendor_text,
            subject=f'New ticket sale — {order.event_name}',
        )
        sent |= _send(
            'sms', vendor.phone,
            f'New ticket sale: {order.event_name}, {when}, '
            f'{order.ticket_count} ticket(s), {who}. Booking ID {order.id}',
        )
    return sent
