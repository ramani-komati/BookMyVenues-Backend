"""
Server-side ticket pricing.

THE RULE: the client sends QUANTITIES, never money. Every price comes from
the event's own TicketType rows and the platform fee comes from the admin's
Settings — exactly the posture bookings/views.compute_amount takes, where the
client's `amount` is only ever accepted, never trusted.

The quote is computed once here and reused by the order endpoint, so the
figure the customer is shown and the figure Razorpay charges are the same
number from the same code path.
"""
from bookings.views import _booking_fee

from .inventory import MAX_TICKETS_PER_ORDER
from .models import TicketType


class PricingError(ValueError):
    """Invalid ticket selection. `code` is echoed to the client."""

    def __init__(self, message, code=''):
        super().__init__(message)
        self.code = code


def _quantity(raw, label):
    """A ticket count from JSON. Strings ('2') are coerced — the rest of the
    API does the same — but anything negative or fractional is refused: a
    negative quantity would subtract from the bill."""
    if raw in (None, ''):
        return 0
    try:
        count = int(str(raw).strip())
    except (TypeError, ValueError):
        raise PricingError(f'{label} quantity must be a whole number.', code='BAD_QUANTITY')
    if count < 0:
        raise PricingError(f'{label} quantity cannot be negative.', code='BAD_QUANTITY')
    return count


def parse_ticket_request(event, raw_tickets):
    """Normalise the client's ticket selection into {key: qty}.

    Accepts either the map shape {"adult": 2, "child": 1} or the list shape
    [{"key": "adult", "qty": 2}] — the customer app sends the map, but the
    list is what a vendor-side tool would naturally post, and tolerating both
    costs four lines.
    """
    if isinstance(raw_tickets, dict):
        pairs = list(raw_tickets.items())
    elif isinstance(raw_tickets, list):
        pairs = [
            (item.get('key'), item.get('qty', item.get('quantity')))
            for item in raw_tickets
            if isinstance(item, dict)
        ]
    else:
        raise PricingError('tickets must be an object or a list.', code='BAD_TICKETS')

    wanted = {}
    for key, raw_qty in pairs:
        key = str(key or '').strip().lower()
        if not key:
            continue
        count = _quantity(raw_qty, key)
        if count:
            wanted[key] = wanted.get(key, 0) + count
    if not wanted:
        raise PricingError('Select at least one ticket.', code='NO_TICKETS')
    return wanted


def quote(event, raw_tickets):
    """Price a ticket selection.

    Returns {items, ticketCount, seats, subtotal, fee, amount}. Raises
    PricingError for anything the customer may not buy.
    """
    wanted = parse_ticket_request(event, raw_tickets)

    types = {
        t.key: t
        for t in TicketType.objects.filter(event=event, is_active=True)
    }
    unknown = sorted(set(wanted) - set(types))
    if unknown:
        # Naming the key rather than silently dropping it: a dropped line
        # would quietly charge less than the customer thinks they are buying.
        raise PricingError(
            f'Unknown ticket type: {unknown[0]}.', code='UNKNOWN_TICKET_TYPE'
        )

    items, subtotal, seats, ticket_count = [], 0, 0, 0
    for ticket_type in sorted(types.values(), key=lambda t: (t.sort_order, t.id)):
        count = wanted.get(ticket_type.key, 0)
        if not count:
            continue
        if ticket_type.max_per_order and count > ticket_type.max_per_order:
            raise PricingError(
                f'At most {ticket_type.max_per_order} {ticket_type.label} '
                f'ticket{"" if ticket_type.max_per_order == 1 else "s"} per order.',
                code='MAX_PER_ORDER',
            )
        line = ticket_type.price * count
        items.append({
            'key': ticket_type.key,
            'label': ticket_type.label,
            'qty': count,
            'price': ticket_type.price,
            'seatsPerTicket': ticket_type.seats_per_ticket,
            'subtotal': line,
        })
        subtotal += line
        seats += ticket_type.seats_per_ticket * count
        ticket_count += count

    if not items:
        raise PricingError('Select at least one ticket.', code='NO_TICKETS')
    if ticket_count > MAX_TICKETS_PER_ORDER:
        raise PricingError(
            f'At most {MAX_TICKETS_PER_ORDER} tickets per order.',
            code='MAX_TICKETS',
        )

    # One flat platform fee per ORDER, not per ticket — the same ₹ figure a
    # venue booking pays, read from the same Settings.effective_fee().
    fee = _booking_fee()
    return {
        'items': items,
        'ticketCount': ticket_count,
        'seats': seats,
        'subtotal': subtotal,
        'fee': fee,
        'amount': subtotal + fee,
    }
