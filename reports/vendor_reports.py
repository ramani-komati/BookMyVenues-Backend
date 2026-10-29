"""
Vendor exports — GET /api/vendors/me/exports/<kind>.

  kind      dashboard tab   filters
  --------  -------------   --------------------------------------------
  bookings  Bookings        venue, status (upcoming | completed |
                            refund_pending), from, to (booking date), q
  earnings  Earnings        venue, from, to (booking date)
  events    My events       —
  venues    My venues       —

ISOLATION: every query starts from the token's own account
(`listing__vendor=request.user`, `request.user.listings`, or
events.vendor_views.visible_events). A `venue` filter is honoured only when
that listing belongs to the caller; anyone else's id is refused with the same
message as an id that does not exist, so it cannot be used to probe.

The Bookings export lists exactly the rows the Bookings tab lists (the
dashboard's set: no unpaid holds, failed attempts, cancellations or refunded
bookings). Refunds are the Earnings report's business — it shows refunded
bookings too, with the customer's number left out, as the dashboard never
showed it.
"""
import datetime
import uuid
from collections import defaultdict

from bookings.models import Booking
from venues.models import Listing

from . import common as c
from .sheets import VENDOR_BOOKING_COLUMNS, event_sheets, online_share, vendor_booking_row
from .workbook import DATETIME, INT, MONEY, Cell, Sheet, col

REPORTS = {}


class Report:
    def __init__(self, kind, prefix, noun, parse, build):
        self.kind = kind
        self.prefix = prefix
        self.noun = noun
        self.parse = parse        # (params, user) -> filters
        self.build = build        # (filters, user) -> (sheets, row count)


def report(kind, prefix, noun, parse):
    def register(build):
        REPORTS[kind] = Report(kind, prefix, noun, parse, build)
        return build
    return register


SEP = ' — '


def _unit_of(listing):
    return str(((listing.record or {}).get('detail') or {}).get('unitOf') or '').strip()


def _name(listing):
    return listing.name or (listing.record or {}).get('name') or ''


def _venue_param(params, user):
    """The `venue` filter -> the set of the caller's listing ids it covers
    (the venue and its pitch/screen unit listings), or None for all venues."""
    raw = str(params.get('venue') or '').strip()
    if not raw or raw == 'All':
        return None
    try:
        venue_id = str(uuid.UUID(raw))
    except ValueError:
        raise c.ExportFilterError('Unknown venue.')
    own = list(user.listings.exclude(status=Listing.Status.DELETED))
    base = next((l for l in own if str(l.id) == venue_id), None)
    if base is None:
        # Not the caller's venue (or no such venue): the same answer either way.
        raise c.ExportFilterError('Unknown venue.')
    prefix = _name(base) + SEP
    return {
        str(l.id) for l in own
        if str(l.id) == venue_id or _unit_of(l) == venue_id
        or (_name(base) and _name(l).startswith(prefix))
    }


def _base_names(user):
    """{listing id: the venue name a unit listing folds into}."""
    own = list(user.listings.all())
    by_id = {str(l.id): l for l in own}
    names = {}
    for listing in own:
        base = by_id.get(_unit_of(listing))
        name = _name(base) if base else _name(listing).split(SEP)[0]
        names[str(listing.id)] = name
    return names


def _dashboard_bookings(user):
    """The vendor dashboard's booking set (bookings.vendor_views): the
    caller's venues only, without dead rows or unpaid holds."""
    return Booking.objects.filter(listing__vendor=user).exclude(
        status__in=Booking.DEAD_STATUSES
    ).exclude(status='payment_pending')


def _by_date(queryset, start, end):
    if start:
        queryset = queryset.filter(date__gte=start)
    if end:
        queryset = queryset.filter(date__lte=end)
    return queryset


# =====================================================================
# Bookings
# =====================================================================

BOOKING_STATUSES = ('all', 'upcoming', 'completed', 'refund_pending', 'cancelled')


def bookings_filters(params, user):
    start, end = c.date_range(params)
    return {
        'venue': _venue_param(params, user),
        'status': c.choice(params, 'status', BOOKING_STATUSES, 'all'),
        'from': start,
        'to': end,
        'q': c.query(params),
    }


def _status_matches(booking, status):
    # The tab's filter reads each row's display status (as_record 'status').
    shown = booking.display_status
    return (
        status == 'all'
        or (status == 'upcoming' and shown == 'confirmed')
        or (status == 'completed' and shown == 'completed')
        or (status == 'refund_pending' and shown == 'refund_pending')
        or (status == 'cancelled' and shown in ('cancelled', 'refunded'))
    )


@report('bookings', 'vendor_bookings', 'bookings', bookings_filters)
def bookings_report(f, user):
    if f['status'] == 'cancelled':
        # Paid, then cancelled or refunded — the tab's "Cancelled & refunded"
        # filter (bookings.vendor_views.closed_bookings).
        from bookings.vendor_views import closed_bookings
        base = closed_bookings(user)
    else:
        base = _dashboard_bookings(user)
    queryset = _by_date(base, f['from'], f['to'])
    if f['venue'] is not None:
        queryset = queryset.filter(listing_id__in=f['venue'])
    rows = [
        b for b in queryset.order_by('-created_at')
        if _status_matches(b, f['status'])
        # The tab's search box: booking ID, customer, phone, venue, pitch.
        and c.matches(f['q'], [b.id, b.customer_name, b.phone, b.venue_name, b.unit_label])
    ]
    c.check_size(len(rows))
    return [Sheet('Bookings', VENDOR_BOOKING_COLUMNS,
                  (vendor_booking_row(b) for b in rows))], len(rows)


# =====================================================================
# Earnings report
# =====================================================================

def earnings_filters(params, user):
    start, end = c.date_range(params)
    return {'venue': _venue_param(params, user), 'from': start, 'to': end}


def _blank_totals():
    return {
        'kept': 0, 'gross': 0, 'fees': 0, 'promo': 0, 'net': 0, 'net_platform': 0,
        'net_walk_in': 0, 'online': 0, 'cash': 0, 'refund_pending': 0,
        'refund_pending_amount': 0, 'refunded': 0, 'refunded_amount': 0,
    }


def _add(totals, b):
    if c.is_kept(b.status):
        net = c.vendor_net(b)
        totals['kept'] += 1
        totals['gross'] += b.amount
        totals['fees'] += b.fee
        totals['promo'] += c.promo_top_up(b)
        totals['net'] += net
        totals['net_walk_in' if c.is_walk_in(b) else 'net_platform'] += net
        totals['online'] += online_share(b)
        totals['cash'] += c.venue_cash_due(b)
    elif b.status == 'refund_pending':
        totals['refund_pending'] += 1
        totals['refund_pending_amount'] += online_share(b)
    elif b.status == 'refunded':
        amount, _basis = c.refund_figures(b.status, b.refund_amount, c.venue_captured(b))
        totals['refunded'] += 1
        totals['refunded_amount'] += amount or 0


BREAKDOWN_COLUMNS = [
    col('Kept Bookings', 10, INT),
    col('Gross Booking Value', 15, MONEY),
    col('Platform Fees', 13, MONEY),
    col('Platform Promo Top-ups', 13, MONEY),
    col('Net Earnings', 14, MONEY),
    col('Paid Online', 13, MONEY),
    col('Collected at Venue', 13, MONEY),
    col('Refund Pending Bookings', 11, INT),
    col('Refund Pending Amount', 13, MONEY),
    col('Refunded Bookings', 10, INT),
    col('Refunded Amount', 13, MONEY),
]


def _breakdown(t):
    return [
        t['kept'], t['gross'], t['fees'], t['promo'], t['net'], t['online'], t['cash'],
        t['refund_pending'], t['refund_pending_amount'], t['refunded'], t['refunded_amount'],
    ]


@report('earnings', 'vendor_earnings', 'bookings', earnings_filters)
def earnings_report(f, user):
    # Kept bookings, plus the ones being or already refunded — the money the
    # dashboard's earnings exclude, shown here so the report reconciles.
    queryset = Booking.objects.filter(listing__vendor=user).exclude(
        status__in=('removed', 'failed', 'cancelled', 'payment_pending')
    )
    queryset = _by_date(queryset, f['from'], f['to'])
    if f['venue'] is not None:
        queryset = queryset.filter(listing_id__in=f['venue'])
    bookings = list(queryset.order_by('-date', '-created_at'))
    c.check_size(len(bookings))

    names = _base_names(user)
    total = _blank_totals()
    per_venue = defaultdict(_blank_totals)
    per_month = defaultdict(_blank_totals)
    for b in bookings:
        _add(total, b)
        _add(per_venue[names.get(str(b.listing_id), b.venue_name.split(SEP)[0])], b)
        _add(per_month[b.date.replace(day=1)], b)

    if f['from'] or f['to']:
        period = (
            f"{f['from'].strftime('%d %b %Y') if f['from'] else 'your first booking'} to "
            f"{f['to'].strftime('%d %b %Y') if f['to'] else 'the latest booking'}"
            ' (booking date)'
        )
    else:
        period = 'All time'
    venue_label = 'All venues'
    if f['venue'] is not None:
        venue_label = ', '.join(sorted({names.get(i, '') for i in f['venue']} - {''}))

    def money(label, value, note):
        return [label, Cell(value, MONEY), note]

    def count(label, value, note):
        return [label, Cell(value, INT), note]

    summary = [
        ['Report period', period, ''],
        ['Venues', venue_label, ''],
        ['Generated', datetime.datetime.now(tz=c.IST), ''],
        count('Kept bookings', total['kept'],
              'Bookings that still stand. Cancelled, refund-pending and refunded bookings are not counted.'),
        money('Gross booking value', total['gross'],
              'What customers were charged for kept bookings, platform fee included.'),
        money('Less: platform fees', total['fees'],
              'The booking fee TheBookMyVenues keeps on each booking (walk-ins carry none).'),
        money('Plus: platform promo top-ups', total['promo'],
              'Discounts on TheBookMyVenues offers, paid back to you. Your own venue offers are not topped up.'),
        money('Net earnings', total['net'],
              'Gross − platform fees + promo top-ups. With no filters this equals "Total earnings" on your dashboard.'),
        money('   from platform bookings', total['net_platform'], ''),
        money('   from walk-ins', total['net_walk_in'], ''),
        money('Paid online', total['online'],
              'The part of kept bookings paid through the platform (settled to you in weekly payouts, less fees).'),
        money('Collected at venue', total['cash'],
              'Paid to you in person: walk-ins, pay-at-venue, and the venue share of part payments.'),
        count('Refund pending — bookings', total['refund_pending'],
              'Cancelled bookings waiting for their refund. Not part of your earnings.'),
        money('Refund pending — amount', total['refund_pending_amount'],
              'Paid online, being returned to the customer.'),
        count('Refunded — bookings', total['refunded'], ''),
        money('Refunded — amount', total['refunded_amount'],
              'Refund recorded on each booking; where none was recorded, the full amount paid online.'),
    ]

    def detail_rows():
        for b in bookings:
            # Refunded bookings are not on the dashboard, so neither is the
            # customer's number for them.
            yield vendor_booking_row(b, show_phone=b.status != 'refunded')

    sheets = [
        Sheet('Summary', [col('Metric', 34), col('Value', 18), col('Notes', 90)],
              summary, filters=False),
        Sheet('By Venue', [col('Venue', 30), *BREAKDOWN_COLUMNS],
              [[name, *_breakdown(t)] for name, t in sorted(per_venue.items())]),
        Sheet('By Month', [col('Month', 12, 'mmm yyyy'), *BREAKDOWN_COLUMNS],
              [[month, *_breakdown(t)] for month, t in sorted(per_month.items(), reverse=True)]),
        Sheet('Bookings', VENDOR_BOOKING_COLUMNS, detail_rows()),
    ]
    return sheets, len(bookings)


# =====================================================================
# Events and ticket sales
# =====================================================================

def no_filters(params, user):
    return {}


@report('events', 'vendor_event_sales', 'events', no_filters)
def events_report(f, user):
    from events.models import EventOrder
    from events.vendor_views import visible_events

    # Exactly the events the My events tab shows (own events in any state,
    # plus published events at the vendor's own venues).
    events = sorted(visible_events(user).select_related('vendor'),
                    key=lambda e: e.created_at, reverse=True)
    c.check_size(len(events))
    sheets = event_sheets(events, owner=user)

    # Ticket buyers: the same rows the per-event Bookings list shows the
    # vendor (events.vendor_views.VendorEventOrdersView) — no unpaid holds.
    orders = EventOrder.objects.filter(event__in=[e.id for e in events]).exclude(
        status__in=('payment_pending', 'removed')
    ).order_by('-created_at')
    count = orders.count()
    c.check_size(count)

    def order_rows():
        for o in orders.iterator(chunk_size=2000):
            key = o.display_status
            captured = o.amount if o.razorpay_payment_id else 0
            refunded, _basis = c.refund_figures(o.status, o.refund_amount, captured)
            yield [
                o.id, o.event_name, o.date, c.time_range(o.start_time, o.end_time),
                o.customer_name, o.phone, c.tickets_text(o.items), o.ticket_count,
                o.subtotal, o.fee, o.amount, c.EVENT_STATUS_LABEL.get(key, key),
                refunded, o.checked_in_at, o.created_at,
            ]

    sheets.append(Sheet('Orders', [
        col('Order ID', 16), col('Event', 28), col('Event Date', 13, 'dd-mmm-yyyy'),
        col('Time', 14), col('Customer Name', 22), col('Customer Phone', 14),
        col('Tickets', 26), col('Ticket Count', 9, INT),
        col('Ticket Subtotal', 13, MONEY), col('Platform Fee', 12, MONEY),
        col('Order Total', 13, MONEY), col('Payment Status', 18),
        col('Refund Amount', 13, MONEY), col('Checked In At', 18, DATETIME),
        col('Booked On', 18, DATETIME),
    ], order_rows()))
    return sheets, len(events)


# =====================================================================
# Venues / listings
# =====================================================================

LISTING_STATUS = {
    Listing.Status.LIVE: 'Live',
    Listing.Status.PENDING: 'Pending review',
    Listing.Status.PAUSED: 'Paused by admin',
    Listing.Status.CHANGES: 'Changes requested',
    Listing.Status.REJECTED: 'Rejected',
}


@report('venues', 'vendor_venues', 'listings', no_filters)
def venues_report(f, user):
    from adminpanel.formatters import _district_and_city, _int
    from bookings.ratings import venue_ratings_map

    # The dashboard's venue list: every own listing that is not deleted.
    listings = list(user.listings.exclude(status=Listing.Status.DELETED).order_by('created_at'))
    c.check_size(len(listings))
    ratings = venue_ratings_map(listings)
    by_id = {str(l.id): l for l in listings}

    stats = defaultdict(lambda: [0, 0, 0])    # kept, gross, net
    for b in _dashboard_bookings(user).filter(listing_id__in=list(by_id)):
        if c.is_kept(b.status):
            entry = stats[str(b.listing_id)]
            entry[0] += 1
            entry[1] += b.amount
            entry[2] += c.vendor_net(b)

    columns = [
        col('Listing ID', 38), col('Venue Name', 30), col('Unit Of', 24),
        col('Category', 16), col('Location', 30), col('Locality', 16),
        col('District', 14), col('City', 14), col('Listed Price', 12, MONEY),
        col('Price Unit', 10), col('Listing Status', 17), col('Paused by You', 9),
        col('Deletion Requested At', 18, DATETIME), col('Rating', 8, '0.0'),
        col('Ratings', 8, INT), col('Kept Bookings', 10, INT),
        col('Gross Booking Value', 15, MONEY), col('Net Earnings', 14, MONEY),
        col('Added On', 18, DATETIME), col('Last Updated', 18, DATETIME),
    ]

    def rows():
        for listing in listings:
            record = listing.record or {}
            detail = record.get('detail') or {}
            base = by_id.get(_unit_of(listing))
            district, city = _district_and_city(listing)
            average, count = ratings.get(str(listing.id), (None, 0))
            kept, gross, net = stats.get(str(listing.id), (0, 0, 0))
            yield [
                str(listing.id), _name(listing), _name(base) if base else '',
                listing.category or record.get('category') or '',
                str(record.get('location') or ''), listing.locality or '',
                district, city, _int(record.get('price') or 0),
                str(record.get('unit') or '').replace('/', '').strip(),
                LISTING_STATUS.get(listing.status, listing.status),
                bool(detail.get('paused')), listing.deletion_requested_at,
                average, count, kept, gross, net, listing.created_at, listing.updated_at,
            ]

    return [Sheet('Venues', columns, rows())], len(listings)
