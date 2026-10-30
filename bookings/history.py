"""
The customer's ONE booking history — venue bookings, event tickets and
memberships in a single list, newest booking first.

    GET /api/users/me/booking-history?page=&limit=
        -> {items: [{type, card, id, createdAt, record}], total, page, limit, hasMore}

  type    'venue' | 'event' — what the customer booked (the card's label)
  card    'booking' | 'membership' | 'ticket' — which card shows it
  record  EXACTLY what the per-product endpoints serve for that row
          (GET /users/me/bookings, /users/me/event-orders, /users/me/memberships),
          so every existing card, detail view and action keeps working.

Order: when the booking was MADE (created_at), newest first — never the
visit / event date. Rows made in the same instant fall back to a fixed
order (venue, membership, ticket; then the id, descending) so a refresh never
reshuffles them.

Owner-scoped like every /users/me endpoint: each query filters
user=request.user, so nobody can see another customer's bookings. Each
product keeps its own visibility rule:
  * venue bookings — never-paid / failed / cancelled attempts and
    admin-removed rows are left out (the same rows the website's My bookings
    already hides),
  * memberships — the same rule as GET /users/me/memberships,
  * tickets — every order but admin-removed ones, with its real outcome
    (as GET /users/me/event-orders).
"""
import datetime

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Booking

MAX_LIMIT = 50
DEFAULT_LIMIT = 20

# Venue-booking statuses that are not a booking the customer holds — the
# website's utils/bookingMeta UNCONFIRMED_STATUSES, plus admin-removed rows.
HIDDEN_VENUE_STATUSES = (
    'payment_pending', 'pending', 'awaiting_payment', 'unpaid',
    'failed', 'payment_failed', 'expired', 'abandoned', 'cancelled', 'canceled',
    'removed',
)
HIDDEN_MEMBERSHIP_STATUSES = ('payment_pending', 'failed', 'cancelled', 'removed')

# Tie-break between products for rows made in the same instant.
_RANK = {'booking': 2, 'membership': 1, 'ticket': 0}
_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _made_at(*candidates):
    """The first real timestamp — created_at is always set today, but a
    row without one sorts last instead of breaking the page."""
    for value in candidates:
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value
    return _EPOCH


def _top(queryset, field, need):
    """The newest `need` rows by `field`, plus any rows sharing the last
    one's timestamp — so a page boundary can never split a tie."""
    rows = list(queryset.order_by(f'-{field}')[:need])
    if len(rows) == need and rows:
        last = rows[-1]
        stamp = last
        for part in field.split('__'):
            stamp = getattr(stamp, part)
        seen = {r.pk for r in rows}
        rows.extend(r for r in queryset.filter(**{field: stamp}) if r.pk not in seen)
    return rows


def history_sources(user):
    """The three owner-scoped querysets the history merges."""
    from events.models import EventOrder
    from offerings.models import Membership

    venue = Booking.objects.filter(user=user).exclude(
        status__in=HIDDEN_VENUE_STATUSES,
    ).exclude(kind='membership').select_related('rating', 'listing__vendor')
    memberships = Membership.objects.filter(user=user).select_related('booking').exclude(
        booking__status__in=HIDDEN_MEMBERSHIP_STATUSES,
    )
    tickets = EventOrder.objects.filter(user=user).exclude(status='removed').select_related(
        'event', 'event__vendor', 'rating',
    )
    return venue, memberships, tickets


def _entries(user, need):
    """(sort_key, card, obj) for the newest `need` rows of every product."""
    venue, memberships, tickets = history_sources(user)
    out = []
    for row in _top(venue, 'created_at', need):
        out.append(((_made_at(row.created_at, row.paid_at), _RANK['booking'], str(row.pk)), 'booking', row))
    for row in _top(memberships, 'booking__created_at', need):
        b = row.booking
        out.append(((_made_at(b.created_at, b.paid_at), _RANK['membership'], str(b.pk)), 'membership', row))
    for row in _top(tickets, 'created_at', need):
        out.append(((_made_at(row.created_at, getattr(row, 'paid_at', None)), _RANK['ticket'], str(row.pk)),
                    'ticket', row))
    out.sort(key=lambda entry: entry[0], reverse=True)
    return out


def _item(card, obj):
    from events.customer_views import customer_order_record
    from offerings.views import _membership_row

    from .views import customer_booking_record

    if card == 'booking':
        record = customer_booking_record(obj)
    elif card == 'membership':
        record = _membership_row(obj)
    else:
        record = customer_order_record(obj)
    return {
        'type': 'event' if card == 'ticket' else 'venue',
        'card': card,
        'id': record.get('id'),
        'createdAt': record.get('createdAt'),
        'record': record,
    }


def history_page(user, page=1, limit=DEFAULT_LIMIT):
    venue, memberships, tickets = history_sources(user)
    total = venue.count() + memberships.count() + tickets.count()
    start = (page - 1) * limit
    entries = _entries(user, start + limit)[start:start + limit]
    return {
        'items': [_item(card, obj) for _, card, obj in entries],
        'total': total,
        'page': page,
        'limit': limit,
        'hasMore': start + limit < total,
    }


class MyBookingHistoryView(APIView):
    """GET /api/users/me/booking-history?page=&limit= — see module doc."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int(request.query_params.get('limit', DEFAULT_LIMIT))
            page = int(request.query_params.get('page', 1))
        except ValueError:
            return Response({'message': 'page and limit must be numbers.'}, status=status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= MAX_LIMIT) or not (1 <= page <= 200):
            return Response({'message': 'Invalid page or limit.'}, status=status.HTTP_400_BAD_REQUEST)
        return Response(history_page(request.user, page, limit))
