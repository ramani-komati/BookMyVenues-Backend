"""
Excel export of the super admin's main Bookings page.

GET /api/admin/bookings/export takes the page's own filters and answers an
.xlsx of exactly the rows the page lists for them. The page filters the
bootstrap rows in the browser (SuperAdmin src/features/bookings/
unifiedBookings.js); this module applies the SAME rules on the server, so the
export never ships records the admin did not ask for:

  type    'venue' | 'event' | 'All'
  status  a row's statusKey — the status the panel shows: the backend's
          display status, with an unpaid hold past its window reading
          'expired' (abandoned checkout) for either kind
  date    'All' | 'today' | 'upcoming' | 'past' | 'range' (+ from / to)
  basis   which date the date filter reads: 'scheduled' (the event date or
          the booked slot date — the page's original behaviour) or
          'created' (the day the booking was made, IST)
  q       the search box: a case-insensitive substring of the same fields
          the page searches (ids, booking refs, names, customer, phone,
          gateway ids)
  today   the admin's "today" (YYYY-MM-DD) so today/upcoming/past split on
          the same day the page used; defaults to today in IST

Type, dates and the stored status narrow the query in SQL; the derived
status (clock-based 'completed', 'expired') and the search haystack are then
checked in Python with exactly the page's rules.
"""
import datetime
import re

from bookings.models import Booking
from bookings.slots import IST, today_ist
from events.models import EventOrder

# Excel sheets top out at 1,048,576 rows; well before that a download stops
# being useful. Past this the admin is asked to narrow the filters.
MAX_EXPORT_ROWS = 50_000

TYPES = ('All', 'venue', 'event')
DATE_PRESETS = ('All', 'today', 'upcoming', 'past', 'range')
BASES = ('scheduled', 'created')
# The page's status filter values (unifiedBookings.js STATUS_OPTIONS).
STATUS_KEYS = (
    'confirmed', 'completed', 'payment_pending', 'expired', 'failed',
    'cancelled', 'refund_pending', 'refunded',
)

KIND_LABEL = {'venue': 'Venue booking', 'event': 'Event ticket'}

# Badge labels, as the page shows them (unifiedBookings.js VENUE_BADGE and
# events/orderStatus.js PAYMENT_BADGE).
VENUE_STATUS_LABEL = {
    'confirmed': 'Confirmed',
    'completed': 'Completed',
    'payment_pending': 'Awaiting payment',
    'failed': 'Payment failed',
    'cancelled': 'Cancelled',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
}
EVENT_STATUS_LABEL = {
    'confirmed': 'Paid',
    'completed': 'Paid',
    'payment_pending': 'Awaiting payment',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
    'cancelled': 'Cancelled',
    'failed': 'Payment failed',
}
METHOD_LABEL = {
    'upi': 'UPI', 'card': 'Card', 'netbanking': 'Net banking',
    'venue': 'Pay at venue', 'walk-in': 'Walk-in', 'online': 'Online',
}

COLUMNS = (
    # (header, width, number format or None)
    ('Booking ID', 14, None),
    ('Record ID', 20, None),
    ('Booking Type', 15, None),
    ('Venue / Event Name', 32, None),
    ('Customer Name', 22, None),
    ('Customer Phone', 15, None),
    ('Event / Booking Date', 18, 'dd-mmm-yyyy'),
    ('Time Slot', 26, None),
    ('Ticket / Booking Details', 40, None),
    ('Quantity', 10, '0'),
    ('Booking Amount', 16, '"₹"#,##0'),
    ('Payment Method', 16, None),
    ('Payment Status', 17, None),
    ('Booking Status', 19, None),
    ('Booking Created At', 20, 'dd-mmm-yyyy hh:mm'),
    # The two separate answers the Bookings page badges show (bookings/
    # status_view.py): is the booking happening, and what happened to the
    # money. Appended so every earlier column keeps its place.
    ('Booking State', 15, None),
    ('Payment State', 17, None),
)


class ExportFilterError(ValueError):
    """A filter value the page could never send."""


def _parse_date(value, name):
    value = str(value or '').strip()
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ExportFilterError(f'{name} must be a date (YYYY-MM-DD).')


def parse_filters(params):
    """The query string -> a validated filter dict (ExportFilterError)."""
    kind = params.get('type') or 'All'
    status = params.get('status') or 'All'
    date = params.get('date') or 'All'
    basis = params.get('basis') or 'scheduled'
    if kind not in TYPES:
        raise ExportFilterError('type must be All, venue or event.')
    if status != 'All' and status not in STATUS_KEYS:
        raise ExportFilterError('Unknown booking status.')
    if date not in DATE_PRESETS:
        raise ExportFilterError('Unknown date filter.')
    if basis not in BASES:
        raise ExportFilterError('basis must be scheduled or created.')
    start = _parse_date(params.get('from'), 'from')
    end = _parse_date(params.get('to'), 'to')
    if date == 'range' and start and end and start > end:
        raise ExportFilterError('The start date is after the end date.')
    return {
        'type': kind,
        'status': status,
        'date': date,
        'basis': basis,
        'from': start,
        'to': end,
        'q': str(params.get('q') or '').strip().lower(),
        'today': _parse_date(params.get('today'), 'today') or today_ist(),
    }


# --- matching ------------------------------------------------------

# A 6-character booking ID ("K7M2QX") — shown in full, '#'-prefixed.
_SHORT_ID = re.compile(r'[A-Z0-9]{6}')


def booking_ref(row_id):
    """The panel's short reference for an id (utils/format.js bookingRef).
    A 6-character booking ID — the shared format for venue bookings and event
    orders alike — IS the reference, shown '#'-prefixed."""
    s = str(row_id or '')
    if _SHORT_ID.fullmatch(s):
        return f'#{s}'
    if len(s) <= 10:
        return s
    for prefix in ('bk_', 'wk_'):
        if s.lower().startswith(prefix):
            s = s[len(prefix):]
            break
    return '#' + s[-6:].upper()


def display_ref(row_id):
    """utils/format.js displayRef: a short event order id as '#K7M2QX';
    any longer id (venue bookings, older event orders) via bookingRef."""
    s = str(row_id or '')
    return f'#{s}' if _SHORT_ID.fullmatch(s) else booking_ref(s)


def _ist_start(day):
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=IST)


def _date_bounds(filters):
    """(first, last) inclusive calendar days the date filter allows, either
    end None for open; or None when the filter is 'All'."""
    date, today = filters['date'], filters['today']
    if date == 'All':
        return None
    if date == 'today':
        return today, today
    if date == 'upcoming':
        return today + datetime.timedelta(days=1), None
    if date == 'past':
        return None, today - datetime.timedelta(days=1)
    return filters['from'], filters['to']   # 'range'


def _narrow(queryset, filters):
    """SQL prefilter: stored status and the date window. Never drops a row
    the page would list — the exact status check happens in Python."""
    queryset = queryset.exclude(status='removed')
    status = filters['status']
    if status in ('confirmed', 'completed'):
        # 'completed' is usually derived from a stored 'confirmed'.
        queryset = queryset.filter(status__in=('confirmed', 'completed'))
    elif status == 'expired':
        queryset = queryset.filter(status='payment_pending')
    elif status != 'All':
        queryset = queryset.filter(status=status)

    bounds = _date_bounds(filters)
    if bounds:
        first, last = bounds
        if filters['basis'] == 'created':
            # Calendar days in IST, the same day the page shows.
            if first:
                queryset = queryset.filter(created_at__gte=_ist_start(first))
            if last:
                queryset = queryset.filter(
                    created_at__lt=_ist_start(last + datetime.timedelta(days=1))
                )
        else:
            if first:
                queryset = queryset.filter(date__gte=first)
            if last:
                queryset = queryset.filter(date__lte=last)
    return queryset


def _venue_row(b):
    status = b.display_status
    key = 'expired' if status == 'payment_pending' and b.hold_expired else status
    label = ('Abandoned checkout' if key == 'expired'
             else VENUE_STATUS_LABEL.get(status, status or 'Unknown'))
    details = []
    if b.unit_label:
        details.append(b.unit_label)
    if b.sport:
        details.append(b.sport)
    addons = ', '.join(
        f"{a.get('name', '')}" + (f" × {a['qty']}" if a.get('qty') not in (None, '', 1) else '')
        for a in (b.addons or []) if a.get('name')
    )
    if addons:
        details.append(f'Add-ons: {addons}')
    method = str(b.method or '').lower()
    return {
        'kind': 'venue',
        'id': b.id,
        'statusKey': key,
        'createdAt': b.created_at,
        'states': b.state_fields(),
        # The page's haystack, field for field (unifiedBookings.js
        # matchesQuery): id, ref, displayRef, title, venue, eventName,
        # location, customer, phone, gateway order, payment.
        'search': [
            b.id, booking_ref(b.id), display_ref(b.id), b.venue_name,
            b.venue_name, None, None, b.customer_name, b.phone, None,
            b.razorpay_payment_id,
        ],
        'cells': [
            display_ref(b.id),
            b.id,
            KIND_LABEL['venue'],
            b.venue_name,
            b.customer_name,
            b.phone,
            b.date,
            ', '.join(b.slots or []),
            ' · '.join(details),
            None,                      # quantity: not a ticket purchase
            b.amount,
            METHOD_LABEL.get(method, b.method or ''),
            b.status,                  # the stored lifecycle value, verbatim
            label,
        ],
    }


def _event_row(o):
    key = o.display_status
    if o.status == 'payment_pending' and key == 'expired':
        label = 'Abandoned checkout'
    elif o.status == 'confirmed' and key == 'completed':
        label = 'Completed'
    else:
        label = EVENT_STATUS_LABEL.get(o.status, o.status or 'Unknown')
    start = o.start_time.strftime('%H:%M') if o.start_time else ''
    end = o.end_time.strftime('%H:%M') if o.end_time else ''
    return {
        'kind': 'event',
        'id': o.id,
        'statusKey': key,
        'createdAt': o.created_at,
        'states': o.state_fields(),
        'search': [
            o.id, booking_ref(o.id), display_ref(o.id),
            o.event_name, None, o.event_name, o.location, o.customer_name,
            o.phone, o.razorpay_order_id, o.razorpay_payment_id,
        ],
        'cells': [
            display_ref(o.id),
            o.id,
            KIND_LABEL['event'],
            o.event_name,
            o.customer_name,
            o.phone,
            o.date,
            f'{start} – {end}' if start and end else start,
            ', '.join(
                f"{i.get('qty', 0)}× {i.get('label', '')}" for i in (o.items or [])
            ),
            o.ticket_count,
            o.amount,
            # Event orders are paid through Razorpay checkout only; the
            # instrument is not stored, so say 'Online' only once a payment
            # was actually recorded (as the page does).
            'Online' if o.razorpay_payment_id else '',
            o.status,
            label,
        ],
    }


def _matches(row, filters):
    if filters['status'] != 'All' and row['statusKey'] != filters['status']:
        return False
    q = filters['q']
    if q:
        haystack = ' '.join(str(v) for v in row['search'] if v).lower()
        if q not in haystack:
            return False
    return True


def matching_rows(filters, limit=MAX_EXPORT_ROWS):
    """Rows the Bookings page lists for these filters, newest first.

    Returns (rows, total). Venue and event orders live in separate tables
    with separate id spaces, so a row is keyed by kind + id; a key is only
    ever emitted once. Stops collecting at `limit` + 1 so an oversized export
    is refused without building it.
    """
    sources = []
    if filters['type'] in ('All', 'venue'):
        sources.append((_narrow(Booking.objects.all(), filters), _venue_row))
    if filters['type'] in ('All', 'event'):
        sources.append((_narrow(EventOrder.objects.all(), filters), _event_row))

    rows, seen = [], set()
    for queryset, build in sources:
        for record in queryset.order_by('-created_at').iterator(chunk_size=1000):
            row = build(record)
            key = (row['kind'], row['id'])
            if key in seen or not _matches(row, filters):
                continue
            seen.add(key)
            rows.append(row)
            if len(rows) > limit:
                return rows, len(rows)
    # One list, newest first; the sort is stable so venue rows keep their
    # place ahead of event rows on an identical timestamp (as on the page).
    rows.sort(key=lambda r: r['createdAt'], reverse=True)
    return rows, len(rows)


# --- workbook ------------------------------------------------------

def build_workbook(rows):
    """The .xlsx bytes: bold frozen header row with filters, sized columns,
    real dates and rupee amounts. Written by reports/workbook.py, the writer
    every other dashboard export shares (it also keeps customer-typed text
    from ever being read as a formula)."""
    from reports.workbook import Sheet, build

    return build([Sheet(
        'Bookings', COLUMNS,
        (
            list(row['cells']) + [
                row['createdAt'],
                row['states']['bookingStateLabel'],
                row['states']['paymentStateLabel'],
            ]
            for row in rows
        ),
    )])


def export_filename(today=None):
    return f'bookmyvenues_bookings_{(today or today_ist()).isoformat()}.xlsx'
