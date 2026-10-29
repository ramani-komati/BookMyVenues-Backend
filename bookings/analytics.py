"""
Vendor business insights — the `analytics` block of GET /vendors/me/dashboard.

Every figure comes from stored rows; nothing is estimated. The money words
are the ones reports/common.py defines (so the dashboard, the Excel exports
and payouts never disagree):

  bookingValue   Σ amount of bookings that still stand ("kept"): what
                 customers were quoted, platform fee included
  captured       Σ what the payment gateway actually charged (rows with a
                 Razorpay payment id — whatever happened to them later)
  refunded       Σ refunds recorded on refunded rows (the full captured
                 amount when an admin refunded without typing one)
  net            Σ the vendor's earnings on kept bookings: amount +
                 platform-promo top-up − the booking's frozen fee
                 (vendor_views.booking_net — the payout formula)

Windows are rolling and keyed on the BOOKED date (when the venue is used),
like the existing earnings cards: today, the last 7 days, the last 30 days.

Occupancy = booked minutes ÷ open minutes over the last 30 days up to today,
per venue, using the venue's own opening hours (venues/business.py). A venue
with no open minutes in the window reports null — never a made-up 0 %.
"""
import datetime
from collections import Counter, defaultdict

from django.utils import timezone

from reports.common import is_kept, refund_figures, venue_captured

from .models import Booking, Rating
from .slots import SlotError, now_minutes_ist, parse_slots, today_ist
from .vendor_views import booking_net

TOP_N = 5
OCCUPANCY_DAYS = 30
POPULAR_DAYS = 90


def _base_name(booking):
    name = booking.venue_name or ''
    return name.rsplit(' — ', 1)[0] if ' — ' in name else name


def _money(rows):
    value = captured = refunded = net = 0
    for row in rows:
        cap = venue_captured(row)
        captured += cap
        refund, _basis = refund_figures(row.status, row.refund_amount, cap)
        refunded += refund or 0
        if is_kept(row.status):
            value += row.amount
            net += booking_net({
                'amount': row.amount, 'fee': row.fee,
                'discount_amount': row.discount_amount, 'offer': row.offer,
            })
    return {'bookingValue': value, 'captured': captured, 'refunded': refunded, 'net': net}


def _occupancy(listings, kept, today):
    from venues.business import base_listing, open_periods
    from venues.models import VenueBusinessSettings

    start = today - datetime.timedelta(days=OCCUPANCY_DAYS - 1)
    by_listing = defaultdict(int)
    now = now_minutes_ist()
    for booking in kept:
        if not (start <= booking.date <= today) or booking.listing_id is None:
            continue
        try:
            intervals = parse_slots(booking.slots)
        except SlotError:
            continue
        for s, e in intervals:
            if booking.date == today:
                e = min(e, max(s, now))       # only the part already in the past
            by_listing[booking.listing_id] += max(0, e - s)

    venues = defaultdict(lambda: {'name': '', 'booked': 0, 'open': 0})
    settings_cache = {}
    for listing in listings:
        if listing.status != 'live':
            continue
        base = base_listing(listing)
        if base.pk not in settings_cache:
            settings_cache[base.pk] = VenueBusinessSettings.objects.filter(listing=base).first()
        settings = settings_cache[base.pk]
        first = max(start, timezone.localtime(listing.created_at).date())
        open_minutes = 0
        day = first
        while day <= today:
            for s, e in open_periods(settings, day)[0]:
                if day == today:
                    e = min(e, max(s, now))
                open_minutes += max(0, e - s)
            day += datetime.timedelta(days=1)
        row = venues[base.pk]
        row['name'] = base.name
        row['open'] += open_minutes
        row['booked'] += by_listing.get(listing.pk, 0)

    out = []
    for venue_id, row in venues.items():
        percent = round(row['booked'] / row['open'] * 100) if row['open'] else None
        out.append({
            'venueId': str(venue_id), 'venue': row['name'],
            'bookedHours': round(row['booked'] / 60, 1), 'openHours': round(row['open'] / 60, 1),
            'percent': percent,
        })
    out.sort(key=lambda r: (r['percent'] is None, -(r['percent'] or 0)))
    return {'days': OCCUPANCY_DAYS, 'venues': out}


def _popular_hours(kept, today):
    since = today - datetime.timedelta(days=POPULAR_DAYS)
    hours = Counter()
    for booking in kept:
        if booking.date < since:
            continue
        try:
            intervals = parse_slots(booking.slots)
        except SlotError:
            continue
        for s, e in intervals:
            for hour in range(s // 60, (e + 59) // 60):
                hours[hour] += 1
    return [
        {'start': f'{h:02d}:00', 'end': f'{(h + 1) % 24:02d}:00', 'bookings': n}
        for h, n in sorted(hours.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
    ]


def _events_summary(user):
    from events.inventory import seats_sold
    from events.models import Event, EventOrder

    today = today_ist()
    events = Event.objects.filter(vendor=user).exclude(status=Event.Status.DELETED)
    sold = remaining = upcoming_dates = 0
    rows = []
    for event in events.prefetch_related('dates'):
        event_sold = event_remaining = 0
        for event_date in event.dates.all():
            count = seats_sold(event_date)
            event_sold += count
            if event_date.date >= today:
                upcoming_dates += 1
                event_remaining += max(0, event_date.capacity - count)
        sold += event_sold
        remaining += event_remaining
        rows.append({'id': str(event.id), 'name': event.name, 'status': event.status,
                     'seatsSold': event_sold, 'seatsRemaining': event_remaining})
    orders = EventOrder.objects.filter(event__vendor=user, status='confirmed')
    rows.sort(key=lambda r: -r['seatsSold'])
    return {
        'count': len(rows),
        'seatsSold': sold,
        'seatsRemaining': remaining,
        'upcomingDates': upcoming_dates,
        'ticketRevenue': sum(o.subtotal for o in orders),
        'top': rows[:TOP_N],
    }


def _ratings_summary(user):
    from .ratings import visible_ratings

    rows = list(
        visible_ratings().filter(listing__vendor=user).values_list('stars', flat=True)
    ) + list(
        visible_ratings().filter(event__vendor=user).values_list('stars', flat=True)
    )
    if not rows:
        return {'average': None, 'count': 0, 'distribution': {str(i): 0 for i in range(1, 6)}}
    distribution = Counter(rows)
    return {
        'average': round(sum(rows) / len(rows), 1),
        'count': len(rows),
        'distribution': {str(i): distribution.get(i, 0) for i in range(1, 6)},
    }


def vendor_analytics(user, listings):
    """The analytics block for one vendor. `listings` = their non-deleted
    listings (the dashboard already loaded them)."""
    today = today_ist()
    week_start = today - datetime.timedelta(days=6)
    month_start = today - datetime.timedelta(days=29)

    rows = list(
        Booking.objects.filter(listing__vendor=user).exclude(status='removed')
    )
    hold_cutoff = timezone.now() - datetime.timedelta(minutes=30)
    kept = [b for b in rows if is_kept(b.status)]

    def count(start, end):
        return sum(1 for b in kept if start <= b.date <= end)

    statuses = Counter(b.status for b in rows)
    live_holds = sum(
        1 for b in rows if b.status == 'payment_pending' and b.created_at >= hold_cutoff
    )
    channel = Counter(
        'walkIn' if (b.walk_in or b.method == Booking.Method.WALK_IN) else 'platform'
        for b in kept if month_start <= b.date <= today
    )
    venues = Counter(_base_name(b) for b in kept if b.date >= today - datetime.timedelta(days=POPULAR_DAYS))

    return {
        'bookings': {
            'today': count(today, today),
            'week': count(week_start, today),
            'month': count(month_start, today),
            'upcoming': sum(1 for b in kept if b.date > today),
        },
        'statuses': {
            'confirmed': len(kept),
            'awaitingPayment': live_holds,
            'cancelled': statuses.get('cancelled', 0),
            'failed': statuses.get('failed', 0),
            'refundPending': statuses.get('refund_pending', 0),
            'refunded': statuses.get('refunded', 0),
        },
        'money': {
            'month': _money([b for b in rows if month_start <= b.date <= today]),
            'allTime': _money(rows),
        },
        'channels': {'platform': channel.get('platform', 0), 'walkIn': channel.get('walkIn', 0)},
        'topVenues': [
            {'venue': name, 'bookings': n} for name, n in venues.most_common(TOP_N) if name
        ],
        'popularHours': _popular_hours(kept, today),
        'occupancy': _occupancy(listings, kept, today),
        'events': _events_summary(user),
        'ratings': _ratings_summary(user),
        'windows': {'popularDays': POPULAR_DAYS, 'occupancyDays': OCCUPANCY_DAYS},
    }
