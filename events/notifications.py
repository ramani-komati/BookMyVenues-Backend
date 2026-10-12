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


def notify_tickets_confirmed(order):
    """Tell the CUSTOMER their tickets are booked and the VENDOR they sold
    some. Returns True when at least one message got through, so the caller
    can mark it sent and a later catch-up run can retry the rest."""
    when = _when(order)
    tickets = _ticket_lines(order)
    sent = False

    customer_text = (
        f'Your tickets are confirmed.\n\n'
        f'{order.event_name}\n{when}\n{order.location}\n\n'
        f'Tickets: {tickets}\nPaid: ₹{order.amount:,}\n'
        f'Order ID: {order.id}\n\n'
        f'Show this order ID at the entry gate.'
    )
    sent |= _send(
        'email', getattr(order.user, 'email', '') or '', customer_text,
        subject=f'Tickets confirmed — {order.event_name}',
    )
    sent |= _send(
        'sms', order.phone,
        f'Tickets confirmed: {order.event_name}, {when}. '
        f'{order.ticket_count} ticket(s). ID {order.id}',
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
            f'Phone: {order.phone}\nOrder ID: {order.id}'
        )
        sent |= _send(
            'email', vendor.email or '', vendor_text,
            subject=f'New ticket sale — {order.event_name}',
        )
        sent |= _send(
            'sms', vendor.phone,
            f'New ticket sale: {order.event_name}, {when}, '
            f'{order.ticket_count} ticket(s), {who}. ID {order.id}',
        )
    return sent
