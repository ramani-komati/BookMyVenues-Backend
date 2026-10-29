"""
Pieces every export shares: filter parsing, the row cap, file names, and the
money rules for one booking or ticket order.

MONEY — the definitions every report uses, so no two exports disagree:

  booking value   `amount`, the total the customer was quoted (fee included).
  kept            the booking still stands and its money stays with us/the
                  vendor. For a venue booking: any status except the dead
                  ones (cancelled, refunded, failed, removed), an unpaid hold
                  or a pending refund — the exact rule the vendor dashboard's
                  earnings use (NOT_KEPT). For an event order: 'confirmed',
                  the set the Events pages count.
  captured        what the payment gateway actually charged: only rows that
                  carry a Razorpay payment id. A venue booking's online share
                  is `online_amount` (part-paid bookings collect the rest in
                  cash at the venue); an event order is charged in full.
  refunded        the refund recorded on the row. When the admin refunded a
                  booking without typing an amount, the gateway refunded the
                  whole payment, so the captured amount is used and the
                  report says so in a "basis" column. A refunded row with no
                  payment record has no amount to report — left blank, never
                  guessed.
  vendor net      amount + platform-funded promo top-up − the booking's frozen
                  platform fee (bookings.vendor_views.booking_net — the same
                  formula the vendor dashboard and payouts use).
"""
import datetime

from bookings.slots import IST, today_ist

# Excel sheets top out at 1,048,576 rows; well before that a download stops
# being useful. Past this the person is asked to narrow the filters — an
# export is never silently cut short. (Same cap as the Bookings export.)
MAX_ROWS = 50_000

MAX_QUERY_LENGTH = 100

METHOD_LABEL = {
    'upi': 'UPI', 'card': 'Card', 'netbanking': 'Net banking',
    'venue': 'Pay at venue', 'walk-in': 'Walk-in', 'online': 'Online',
}

# A venue booking's stored lifecycle value, as people read it.
VENUE_STATUS_LABEL = {
    'confirmed': 'Confirmed',
    'completed': 'Completed',
    'payment_pending': 'Awaiting payment',
    'expired': 'Abandoned checkout',
    'failed': 'Payment failed',
    'cancelled': 'Cancelled',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
}
EVENT_STATUS_LABEL = {
    'confirmed': 'Paid',
    'completed': 'Paid (event over)',
    'payment_pending': 'Awaiting payment',
    'expired': 'Abandoned checkout',
    'failed': 'Payment failed',
    'cancelled': 'Cancelled',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
}
EVENT_LIFECYCLE_LABEL = {
    'draft': 'Draft',
    'pending': 'Pending review',
    'live': 'Published',
    'paused': 'Unpublished',
    'rejected': 'Rejected',
    'deleted': 'Deleted',
}
REFUND_STATUS_LABEL = {'refund_pending': 'Refund pending', 'refunded': 'Refunded'}

# Venue booking statuses whose money is not kept (see MONEY above). Matches
# bookings.vendor_views (dashboard earnings) exactly.
NOT_KEPT = ('cancelled', 'refunded', 'failed', 'removed', 'payment_pending', 'refund_pending')


def is_kept(status):
    return status not in NOT_KEPT


class ExportFilterError(ValueError):
    """A filter value the page could never send."""


class TooManyRows(Exception):
    """More rows match than one export may carry."""


# --- filters -------------------------------------------------------

def choice(params, name, choices, default):
    value = str(params.get(name) or default)
    if value not in choices:
        raise ExportFilterError(f'Unknown {name} filter.')
    return value


def date_param(params, name):
    value = str(params.get(name) or '').strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ExportFilterError(f'{name} must be a date (YYYY-MM-DD).')


def date_range(params):
    """(from, to) inclusive calendar days, either None; refused when reversed."""
    start, end = date_param(params, 'from'), date_param(params, 'to')
    if start and end and start > end:
        raise ExportFilterError('The start date is after the end date.')
    return start, end


def query(params, name='q'):
    """The search box: trimmed and lower-cased, of a sane length."""
    value = str(params.get(name) or '').strip().lower()
    if len(value) > MAX_QUERY_LENGTH:
        raise ExportFilterError('The search text is too long.')
    return value


def matches(q, values):
    """Case-insensitive substring search across a row's searchable fields."""
    if not q:
        return True
    return q in ' '.join(str(v) for v in values if v not in (None, '')).lower()


def ist_start(day):
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=IST)


def created_between(queryset, start, end, field='created_at'):
    """Rows created on IST calendar days start..end (either end open)."""
    if start:
        queryset = queryset.filter(**{f'{field}__gte': ist_start(start)})
    if end:
        queryset = queryset.filter(
            **{f'{field}__lt': ist_start(end + datetime.timedelta(days=1))}
        )
    return queryset


def check_size(count):
    if count > MAX_ROWS:
        raise TooManyRows()


def filename(prefix, today=None):
    """vendor_bookings_2026-10-24.xlsx — the day in IST."""
    return f'{prefix}_{(today or today_ist()).isoformat()}.xlsx'


def describe(filters):
    """'status=active, from=2026-10-01' — for the audit trail."""
    parts = []
    for key, value in filters.items():
        if value in (None, '', 'All', 'all'):
            continue
        if isinstance(value, datetime.date):
            value = value.isoformat()
        parts.append(f'{key}={value!r}' if key == 'q' else f'{key}={value}')
    return ', '.join(parts) or 'no filters'


# --- money ---------------------------------------------------------

def method_label(method):
    method = str(method or '').lower()
    return METHOD_LABEL.get(method, method)


def is_walk_in(booking):
    return bool(booking.walk_in) or booking.method == 'walk-in'


def venue_captured(booking):
    """₹ the gateway charged for a venue booking (0 without a payment)."""
    return booking.online_amount if booking.razorpay_payment_id else 0


def venue_cash_due(booking):
    """₹ collected in person at the venue rather than online: the whole bill
    for walk-ins and legacy pay-at-venue, the remainder for part payments."""
    if is_walk_in(booking) or booking.method == 'venue':
        return booking.amount
    return booking.venue_due


def refund_figures(status, refund_amount, captured):
    """(amount or None, basis) for a refunded row; (None, '') otherwise."""
    if status != 'refunded':
        return None, ''
    if refund_amount is not None:
        return refund_amount, 'Recorded refund amount'
    if captured:
        return captured, 'Full captured amount (no partial amount recorded)'
    return None, 'No payment record — amount unknown'


def payment_status(booking):
    """How the customer paid, as the vendor dashboard labels it
    (utils/bookingMeta.paymentStatus on the frontend)."""
    if is_walk_in(booking):
        return 'Walk-in (paid at venue)'
    if booking.method == 'venue':
        return 'Pay at venue'
    if booking.pay_now is not None and booking.venue_due > 0 and booking.pay_now < booking.amount:
        return 'Part-paid online'
    return 'Paid online'


def promo_top_up(booking):
    """Platform-funded discount the vendor is paid back (0 for venue offers)."""
    return booking.discount_amount if (booking.offer or {}).get('source') == 'platform' else 0


def offer_funding(booking):
    if not booking.discount_amount:
        return ''
    source = (booking.offer or {}).get('source')
    return 'Platform' if source == 'platform' else 'Venue'


def vendor_net(booking):
    from bookings.vendor_views import booking_net
    return booking_net({
        'amount': booking.amount, 'fee': booking.fee,
        'discount_amount': booking.discount_amount, 'offer': booking.offer,
    })


def addons_text(addons):
    parts = []
    for addon in addons or []:
        name = str((addon or {}).get('name') or '').strip()
        # The frontend's zero-price "Pay at venue" marker is not an add-on.
        if not name or (name == 'Pay at venue' and not (addon.get('price') or 0)):
            continue
        qty = addon.get('qty')
        parts.append(f'{name} × {qty}' if qty not in (None, '', 1) else name)
    return ', '.join(parts)


def tickets_text(items):
    return ', '.join(
        f"{(item or {}).get('qty', 0)}× {(item or {}).get('label', '')}" for item in items or []
    )


def hhmm(value):
    return value.strftime('%H:%M') if value else ''


def time_range(start, end):
    start, end = hhmm(start), hhmm(end)
    return f'{start} – {end}' if start and end else start


# --- event sales ---------------------------------------------------

def event_sales(event_ids):
    """Sales per event and per date, from the orders themselves.

    Returns {event_id: stats} with
      orders / tickets / revenue / fees   CONFIRMED orders only (the same
                                          set the Events pages count);
                                          revenue is the ticket subtotal,
                                          fees the platform fee on top
      by_type {key: [qty, revenue]}       confirmed orders, from line items
      refund_pending [count, ₹ captured]
      refunded [count, ₹ refunded]        (refund_figures rules)
      cancelled_paid [count, ₹ captured]  cancelled with a captured payment
                                          and no refund recorded
    and {event_date_id: [paid seats, seats held in an unexpired checkout]}.
    Paid seats are confirmed + refund-pending orders, as the admin page
    counts them.
    """
    from django.utils import timezone

    from bookings.views import PENDING_HOLD_MINUTES
    from events.models import EventOrder

    cutoff = timezone.now() - datetime.timedelta(minutes=PENDING_HOLD_MINUTES)
    stats, by_date = {}, {}
    orders = EventOrder.objects.filter(event_id__in=list(event_ids)).exclude(
        status='removed'
    ).values_list(
        'event_id', 'event_date_id', 'status', 'seats', 'ticket_count', 'subtotal',
        'fee', 'amount', 'items', 'refund_amount', 'razorpay_payment_id', 'created_at',
    )
    for (event_id, date_id, status, seats, tickets, subtotal, fee, amount, items,
         refund_amount, payment_id, created_at) in orders:
        entry = stats.setdefault(str(event_id), {
            'orders': 0, 'tickets': 0, 'revenue': 0, 'fees': 0, 'by_type': {},
            'refund_pending': [0, 0], 'refunded': [0, 0], 'cancelled_paid': [0, 0],
        })
        captured = amount if payment_id else 0
        if status == 'confirmed':
            entry['orders'] += 1
            entry['tickets'] += tickets or 0
            entry['revenue'] += subtotal or 0
            entry['fees'] += fee or 0
            for item in items or []:
                key = str((item or {}).get('key') or '')
                qty = int((item or {}).get('qty') or 0)
                line = (item or {}).get('subtotal')
                if line is None:
                    line = qty * int((item or {}).get('price') or 0)
                slot = entry['by_type'].setdefault(key, [0, 0])
                slot[0] += qty
                slot[1] += int(line or 0)
        elif status == 'refund_pending':
            entry['refund_pending'][0] += 1
            entry['refund_pending'][1] += captured
        elif status == 'refunded':
            refunded, _basis = refund_figures(status, refund_amount, captured)
            entry['refunded'][0] += 1
            entry['refunded'][1] += refunded or 0
        elif status == 'cancelled' and payment_id:
            entry['cancelled_paid'][0] += 1
            entry['cancelled_paid'][1] += captured

        if date_id:
            seats_row = by_date.setdefault(date_id, [0, 0])
            if status in ('confirmed', 'refund_pending'):
                seats_row[0] += seats or 0
            elif status == 'payment_pending' and created_at >= cutoff:
                seats_row[1] += seats or 0
    return stats, by_date


EMPTY_SALES = {
    'orders': 0, 'tickets': 0, 'revenue': 0, 'fees': 0, 'by_type': {},
    'refund_pending': [0, 0], 'refunded': [0, 0], 'cancelled_paid': [0, 0],
}
