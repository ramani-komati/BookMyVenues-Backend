"""
Platform finance — the ONE place that decides what money was actually
received, for venue bookings and event ticket orders alike.

The super admin's Dashboard reads `dashboard()`; the bootstrap rows (venue
revenue, vendor revenue, a customer's total spent) read `venue_money()` /
`order_money()`. Nothing else should re-derive these numbers.

DEFINITIONS (₹, whole rupees)

  online received   a payment the gateway CAPTURED: the row carries a
                    razorpay_payment_id. That id is only ever written after a
                    signature-verified checkout callback or a signed webhook
                    (bookings/payments.py, events/payments.py), so a booking
                    record or an invoice number alone never counts. The row
                    must also be in a state a capture can be in — confirmed,
                    refund_pending, refunded, or cancelled after paying.
                    Venue booking: its online share (`online_amount`; part-
                    paid bookings collect the rest at the venue). Event
                    order: the whole `amount`.
  cash collected    money taken at the venue AND confirmed by the vendor
                    ticking "cash collected" (Booking.collected): the whole
                    bill for walk-ins and legacy pay-at-venue bookings, the
                    at-venue remainder of a part payment. Only on a booking
                    that still stands (confirmed).
  pending collection  that same cash on a confirmed booking the vendor has
                    NOT marked collected yet. Shown separately, never added
                    to revenue.
  refunds issued    on a refunded row: the refund amount recorded on it, or
                    — when the admin refunded without typing an amount —
                    the full captured amount (reports.common.refund_figures).
  refund pending    captured money owed back (status refund_pending). Still
                    held, so it is inside "received"; listed separately.
  net revenue       online received + cash collected − refunds issued.

  NOT counted       failed attempts, abandoned / expired holds, pending
                    checkouts, cancelled-unpaid rows, admin-removed rows.

DATES (IST calendar days)
  A capture belongs to the day it was paid (paid_at; rows paid before that
  was recorded fall back to their creation time). Cash belongs to the day it
  was marked collected (else the booking's creation). Refunds are reported
  against the payment they return — "refunds on the payments in this
  period" — because refunds carry no timestamp of their own; that keeps net
  revenue = received − refunded for the same set of payments.
  Booking / order COUNTS use the row's payment day when paid, else its
  creation day.

DUPLICATES
  A webhook redelivery or a retried checkout updates the SAME row, so it can
  never add a second payment. As a last guard, a payment id seen on two rows
  of the same table is counted once (`duplicatePaymentRows` reports any).
"""
import datetime
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Q

from bookings.slots import IST, today_ist

from .common import is_walk_in, refund_figures

# States a captured payment can be in. 'failed' / 'payment_pending' with a
# payment id would be inconsistent data — never counted as received.
CAPTURE_STATUSES = ('confirmed', 'refund_pending', 'refunded', 'cancelled')

RANGES = {
    'today': 'Today',
    'yesterday': 'Yesterday',
    '7d': 'Last 7 days',
    '30d': 'Last 30 days',
    'month': 'This month',
    'all': 'All time',
    'custom': 'Custom range',
}
DEFAULT_RANGE = '30d'
# A custom range longer than this is refused (the daily series would be huge).
MAX_CUSTOM_DAYS = 1100


class RangeError(ValueError):
    """A date filter the page could never send."""


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime.date | None     # inclusive, None = open
    end: datetime.date | None       # inclusive, None = open

    @property
    def label(self):
        return RANGES[self.key]

    def contains(self, moment):
        """Does an aware datetime fall on one of the period's IST days?"""
        if moment is None:
            return False
        day = moment.astimezone(IST).date()
        if self.start and day < self.start:
            return False
        if self.end and day > self.end:
            return False
        return True

    def bounds(self):
        """(aware start, aware end-exclusive) for queryset prefilters."""
        start = datetime.datetime.combine(self.start, datetime.time.min, tzinfo=IST) if self.start else None
        end = (
            datetime.datetime.combine(self.end + datetime.timedelta(days=1), datetime.time.min, tzinfo=IST)
            if self.end else None
        )
        return start, end

    def as_json(self):
        return {
            'key': self.key, 'label': self.label,
            'from': self.start.isoformat() if self.start else None,
            'to': self.end.isoformat() if self.end else None,
        }


def resolve_range(key, start=None, end=None, today=None):
    """The Period for a filter key; 'custom' needs from/to (ISO dates)."""
    today = today or today_ist()
    key = str(key or DEFAULT_RANGE)
    if key not in RANGES:
        raise RangeError('Unknown date range.')
    if key == 'today':
        return Period(key, today, today)
    if key == 'yesterday':
        day = today - datetime.timedelta(days=1)
        return Period(key, day, day)
    if key == '7d':
        return Period(key, today - datetime.timedelta(days=6), today)
    if key == '30d':
        return Period(key, today - datetime.timedelta(days=29), today)
    if key == 'month':
        return Period(key, today.replace(day=1), today)
    if key == 'all':
        return Period(key, None, None)

    def parse(value, name):
        try:
            return datetime.date.fromisoformat(str(value or '').strip())
        except ValueError:
            raise RangeError(f'Custom range needs a valid "{name}" date (YYYY-MM-DD).')

    first, last = parse(start, 'from'), parse(end, 'to')
    if first > last:
        raise RangeError('The start date is after the end date.')
    if (last - first).days > MAX_CUSTOM_DAYS:
        raise RangeError('Pick a custom range of three years or less.')
    return Period(key, first, last)


# --- one row's money -------------------------------------------------

@dataclass
class Money:
    online: int = 0              # captured by the gateway
    cash: int = 0                # collected at the venue (confirmed)
    cash_pending: int = 0        # owed at the venue, not marked collected
    refunded: int = 0            # refund issued against this row
    refund_pending: int = 0      # captured, owed back
    refund_basis: str = ''
    cancelled_paid: bool = False  # cancelled with a capture and no refund yet

    @property
    def received(self):
        return self.online + self.cash

    @property
    def net(self):
        return self.received - self.refunded


def _captured(row):
    return bool(row.razorpay_payment_id) and row.status in CAPTURE_STATUSES


def venue_money(booking):
    """Money for one venue Booking (slot, membership, package, surprise)."""
    money = Money()
    if _captured(booking):
        money.online = booking.online_amount or 0
    if booking.status == 'confirmed':
        due = venue_cash_owed(booking)
        if due:
            if booking.collected:
                money.cash = due
            else:
                money.cash_pending = due
    _refunds(money, booking)
    return money


def venue_cash_owed(booking):
    """₹ a vendor takes at the venue for this booking: the whole bill when it
    never went through the gateway, the remainder of a part payment."""
    if (is_walk_in(booking) or booking.method == 'venue') and not booking.razorpay_payment_id:
        return booking.amount or 0
    return booking.venue_due if booking.razorpay_payment_id else 0


def order_money(order):
    """Money for one event ticket order. Tickets are only sold online."""
    money = Money()
    if _captured(order):
        money.online = order.amount or 0
    _refunds(money, order)
    return money


def _refunds(money, row):
    captured = money.online
    if row.status == 'refunded':
        amount, basis = refund_figures('refunded', row.refund_amount, captured)
        # Never more than was received online (a malformed amount can't
        # make net revenue go below what the row brought in).
        money.refunded = min(amount or 0, captured) if captured else 0
        money.refund_basis = basis
    elif row.status == 'refund_pending':
        money.refund_pending = captured
    elif row.status == 'cancelled' and captured:
        money.cancelled_paid = True


def online_moment(row):
    return row.paid_at or row.created_at


def cash_moment(booking):
    return booking.collected_at or booking.created_at


# --- booking / order state for the counts -----------------------------

def row_state(row, money):
    """One bucket per row: paid | awaiting_cash | pending | expired | failed |
    cancelled | refund_pending | refunded | other."""
    status = row.status
    if status == 'refunded':
        return 'refunded'
    if status == 'refund_pending':
        return 'refund_pending'
    if status == 'cancelled':
        return 'cancelled'
    if status == 'failed':
        return 'failed'
    if status == 'payment_pending':
        return 'expired' if row.hold_expired else 'pending'
    if status == 'confirmed':
        if money.online or money.cash:
            return 'paid'
        if money.cash_pending:
            return 'awaiting_cash'
        return 'other'      # a ₹0 or legacy confirmed row with no payment
    return 'other'


def count_moment(row, money):
    if money.online:
        return online_moment(row)
    if money.cash:
        return cash_moment(row)
    return row.created_at


# --- the dashboard ----------------------------------------------------

@dataclass
class _Totals:
    online: int = 0
    cash: int = 0
    refunded: int = 0
    refund_pending: int = 0
    cash_pending: int = 0
    payments: int = 0            # successful payment events (captures + collections)

    def json(self):
        received = self.online + self.cash
        return {
            'received': received,
            'online': self.online,
            'cash': self.cash,
            'refunded': self.refunded,
            'net': received - self.refunded,
            'refundPending': self.refund_pending,
            'pendingCollection': self.cash_pending,
            'payments': self.payments,
        }


@dataclass
class _Counts:
    total: int = 0
    states: dict = field(default_factory=lambda: defaultdict(int))
    cancelled_paid: int = 0
    tickets: int = 0             # event orders only: tickets on PAID orders

    def json(self, with_tickets=False):
        s = self.states
        body = {
            'total': self.total,
            'paid': s['paid'],
            'awaitingCash': s['awaiting_cash'],
            'pendingPayment': s['pending'],
            'expired': s['expired'],
            'failed': s['failed'],
            'cancelled': s['cancelled'],
            'cancelledAfterPayment': self.cancelled_paid,
            'refundPending': s['refund_pending'],
            'refunded': s['refunded'],
            'other': s['other'],
        }
        if with_tickets:
            body['ticketsPaid'] = self.tickets
        return body


def _prefilter(queryset, period, moment_fields):
    """Rows that could have any activity in the period (cheap SQL narrowing;
    the exact per-day test runs in Python on each row)."""
    start, end = period.bounds()
    if start is None and end is None:
        return queryset
    condition = Q()
    for name in moment_fields:
        part = Q()
        if start is not None:
            part &= Q(**{f'{name}__gte': start})
        if end is not None:
            part &= Q(**{f'{name}__lt': end})
        condition |= part
    return queryset.filter(condition)


def _category_label(category):
    from venues.taxonomy import canonical_category
    return canonical_category(category or '') or 'Uncategorised'


def dashboard(period):
    """Every number the super admin Dashboard shows for one period."""
    from bookings.models import Booking
    from events.models import EventOrder

    venue_t, event_t = _Totals(), _Totals()
    venue_c, event_c = _Counts(), _Counts()
    categories = {}
    daily = defaultdict(lambda: {'venue': 0, 'event': 0})
    duplicates = 0

    def add_category(source, label, amount, refunded):
        key = f'{source}:{label.lower()}'
        row = categories.setdefault(key, {
            'key': key, 'label': label, 'source': source,
            'received': 0, 'refunded': 0, 'payments': 0,
        })
        row['received'] += amount
        row['refunded'] += refunded
        row['payments'] += 1

    def add_day(source, moment, amount):
        if amount:
            daily[moment.astimezone(IST).date().isoformat()][source] += amount

    bookings = _prefilter(
        Booking.objects.exclude(status='removed'),
        period, ('created_at', 'paid_at', 'collected_at'),
    )
    seen = set()
    for booking in bookings.iterator(chunk_size=500):
        money = venue_money(booking)
        if money.online and booking.razorpay_payment_id in seen:
            duplicates += 1
            money.online, money.refunded, money.refund_pending = 0, 0, 0
        elif money.online:
            seen.add(booking.razorpay_payment_id)
        label = _category_label(booking.category)
        if money.online and period.contains(online_moment(booking)):
            venue_t.online += money.online
            venue_t.refunded += money.refunded
            venue_t.refund_pending += money.refund_pending
            venue_t.payments += 1
            add_category('venue', label, money.online, money.refunded)
            add_day('venue', online_moment(booking), money.online)
        if money.cash and period.contains(cash_moment(booking)):
            venue_t.cash += money.cash
            venue_t.payments += 1
            add_category('venue', label, money.cash, 0)
            add_day('venue', cash_moment(booking), money.cash)
        if money.cash_pending and period.contains(booking.created_at):
            venue_t.cash_pending += money.cash_pending
        if period.contains(count_moment(booking, money)):
            venue_c.total += 1
            venue_c.states[row_state(booking, money)] += 1
            if money.cancelled_paid:
                venue_c.cancelled_paid += 1

    orders = _prefilter(
        EventOrder.objects.exclude(status='removed'), period, ('created_at', 'paid_at'),
    )
    seen = set()
    for order in orders.iterator(chunk_size=500):
        money = order_money(order)
        if money.online and order.razorpay_payment_id in seen:
            duplicates += 1
            money.online, money.refunded, money.refund_pending = 0, 0, 0
        elif money.online:
            seen.add(order.razorpay_payment_id)
        if money.online and period.contains(online_moment(order)):
            event_t.online += money.online
            event_t.refunded += money.refunded
            event_t.refund_pending += money.refund_pending
            event_t.payments += 1
            add_category('event', order.event_category or 'Event', money.online, money.refunded)
            add_day('event', online_moment(order), money.online)
        if period.contains(count_moment(order, money)):
            event_c.total += 1
            state = row_state(order, money)
            event_c.states[state] += 1
            if money.cancelled_paid:
                event_c.cancelled_paid += 1
            if state == 'paid':
                event_c.tickets += order.ticket_count or 0

    venue, events = venue_t.json(), event_t.json()
    total = {key: venue[key] + events[key] for key in venue}

    received_total = total['received']
    category_rows = sorted(categories.values(), key=lambda r: (-r['received'], r['label'].lower()))
    for row in category_rows:
        row['net'] = row['received'] - row['refunded']
        # Share of all money received in the period (so the rows add up to
        # the "Total successful payments" card), one decimal.
        row['percent'] = round(row['received'] * 100 / received_total, 1) if received_total else 0

    return {
        'period': period.as_json(),
        'totals': total,
        'venue': venue,
        'events': events,
        'counts': {
            'venue': venue_c.json(),
            'events': event_c.json(with_tickets=True),
        },
        'categories': category_rows,
        'daily': [
            {'date': day, 'venue': v['venue'], 'event': v['event']}
            for day, v in sorted(daily.items())
        ],
        'checks': {'duplicatePaymentRows': duplicates},
        'definitions': DEFINITIONS,
    }


# Plain-language definitions the Dashboard shows under its "How these are
# counted" link — the same words as this module's docstring.
DEFINITIONS = {
    'received': 'Online payments the payment gateway captured, plus cash a vendor marked as collected. '
                'Failed, pending, abandoned and cancelled-unpaid bookings are never counted.',
    'online': 'Payments captured online (verified Razorpay payment), venue bookings and event tickets.',
    'cash': 'Cash taken at the venue that the vendor marked "collected" (walk-ins, pay-at-venue, '
            'the at-venue part of a part payment).',
    'refunded': 'Refunds issued on the payments in this period (the recorded refund amount, '
                'or the full payment when no partial amount was recorded).',
    'net': 'Total successful payments minus refunds issued on them.',
    'refundPending': 'Captured money that is owed back to customers and not refunded yet. '
                     'Still included in successful payments until the refund is issued.',
    'pendingCollection': 'Cash still to be collected at the venue for confirmed bookings made in this '
                         'period. Not counted as revenue until the vendor marks it collected.',
    'dates': 'Payments count on the day they were paid (IST); cash on the day it was marked collected. '
             'Booking and order counts use the payment day when paid, otherwise the day the booking was made.',
    'tickets': 'One event order can hold several tickets: orders and tickets are counted separately.',
}
