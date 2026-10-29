"""
Public event browsing (no auth) — the Events category on the home page and
the event detail page.

Mirrors venues/public_views.py: only LIVE rows are ever returned, list reads
are cached ~60s, and the availability read is deliberately NOT cached because
a stale "seats left" would be a lie.
"""
import uuid

from django.db.models import F, Q
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from bookings.slots import SlotError, parse_date, today_ist
from venues.geo import (
    LocationParamError,
    bounding_box,
    coordinates_payload,
    distance_km,
    parse_location_params,
    rank_by_distance,
)

from .catalog import canonical_event_category, event_categories_payload
from .inventory import availability, date_is_sellable, seats_remaining
from .location import place_q, search_q
from .models import Event, EventDate

CACHE_SECONDS = 60
MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _hhmm(value):
    return value.strftime('%H:%M') if value else None


def date_rows(event, *, only_sellable=True, include_counts=False):
    """The event's dates, each flagged bookable or not.

    `only_sellable` drops past dates, closed nights and anything outside the
    selling window — that is what the customer's date picker consumes, so it
    can never offer a date the order endpoint would refuse.

    `include_counts` adds the raw capacity / remaining numbers. It is OFF for
    every customer-facing payload: the client asked that no remaining-seat
    counter be shown, and the surest way to keep one off the screen is for
    the number never to reach the browser. The inventory check itself is
    untouched — events/inventory.reserve_seats still re-reads the sold count
    under a row lock, so capacity is enforced exactly as before. Admin and
    vendor reads pass include_counts=True, where the numbers are the point.
    """
    today = today_ist()
    rows = []
    for event_date in event.dates.all():
        ok, reason = date_is_sellable(event, event_date, today)
        remaining = seats_remaining(event_date)
        if only_sellable and not ok:
            continue
        row = {
            'id': event_date.id,
            'date': event_date.date.isoformat(),
            'soldOut': remaining == 0,
            # A sold-out date stays in the list (greyed out) so the customer
            # can see the night exists; `available` is what gates selection.
            'available': ok and remaining > 0,
            'reason': '' if ok else reason,
            'isPast': event_date.date < today,
        }
        if include_counts:
            row['capacity'] = event_date.capacity
            row['remaining'] = remaining
            row['sold'] = max(0, event_date.capacity - remaining)
        rows.append(row)
    return rows


def event_summary(event, ratings=None):
    """The card shape: poster, name, place, dates, from-price, availability.

    `ratings` is the precomputed {event_id: (avg, count)} map from
    bookings.ratings.event_ratings_map — pass it when rendering a LIST, or
    every card costs an extra query."""
    from bookings.ratings import event_rating

    if ratings is not None:
        average, count = ratings.get(str(event.id), (None, 0))
    else:
        average, count = event_rating(event.id)
    prices = [t.price for t in event.ticket_types.all() if t.is_active]
    dates = date_rows(event)
    return {
        'id': str(event.id),
        'slug': event.slug,
        'name': event.name,
        'category': event.category,
        'description': event.description[:200],
        'image': event.poster_url,
        'poster': event.poster_url,
        # The rest of the event's photos. On the CARD too, not just the detail
        # page, so an event card can run the same photo slideshow a venue card
        # does — one poster was never enough to show what a night looks like.
        'gallery': list(event.gallery or []),
        'location': event.location,
        'venueName': event.venue_name,
        'address': event.address,
        'fullAddress': event.full_address,
        'locality': event.locality,
        'city': event.city,
        # The event's map pin (its own maps link, else its listed venue's),
        # or null — see Event.sync_coordinates.
        'coordinates': coordinates_payload(event.latitude, event.longitude),
        'featured': event.featured,
        'startTime': _hhmm(event.start_time),
        'endTime': _hhmm(event.end_time),
        'dates': dates,
        'nextDate': dates[0]['date'] if dates else None,
        'fromPrice': min(prices) if prices else None,
        'ticketTypes': [
            t.as_record() for t in event.ticket_types.all() if t.is_active
        ],
        # No ticketsLeft / remaining counters — see date_rows' docstring.
        # `soldOut` and `bookable` are what the card needs, and neither of
        # them leaks a number.
        'soldOut': bool(dates) and all(d['soldOut'] for d in dates),
        # No sellable date left at all: the run has finished, or selling has
        # closed. Without this a finished event rendered a "Book tickets"
        # card that could only ever fail at the next step.
        'bookable': any(d['available'] for d in dates),
        'ended': not dates and not event.dates.filter(date__gte=today_ist()).exists(),
        'status': event.status,
        # Genuine customer ratings only (verified-paid tickets for shows that
        # have happened). null / 0 until someone rates it — never a default.
        'rating': average,
        'ratingCount': count,
    }


def event_record(event):
    """The detail shape — the summary plus everything the page needs."""
    record = event_summary(event)
    record.update({
        'description': event.description,
        'mapsLink': event.maps_link,
        # No phone numbers on this public page: the organiser's contact is
        # released only to ticket holders whose payment is verified
        # (bookings/contact.order_contact, in their booking history).
        'terms': event.terms,
        'bookingOpensOn': (
            event.booking_opens_on.isoformat() if event.booking_opens_on else None
        ),
        'bookingClosesOn': (
            event.booking_closes_on.isoformat() if event.booking_closes_on else None
        ),
        'venueId': str(event.listing_id) if event.listing_id else None,
        'organizer': event.organizer,
        # Every date, including past / closed ones, so the detail page can
        # show "ran on …" rather than silently losing nights. Still no
        # counts — `available` / `soldOut` / `isPast` say everything the page
        # needs without putting a seat counter on screen.
        'allDates': date_rows(event, only_sellable=False),
    })
    return record


# Top Events order: the admin's explicit position first, then the newest
# promotion (the order the row always had), then the newest event.
FEATURED_ORDERING = (
    F('featured_order').asc(nulls_last=True), '-featured_at', '-created_at',
)


def featured_events_q(today=None):
    """The promoted events the homepage may show TODAY: promoted, inside
    the admin's optional from/until window (IST, inclusive; a blank side is
    open-ended), with a date still to sell. Callers start from live_events(),
    so a draft, pending, rejected, paused or deleted event never qualifies."""
    today = today or today_ist()
    return (
        Q(featured=True, dates__date__gte=today, dates__is_available=True)
        & (Q(featured_from__isnull=True) | Q(featured_from__lte=today))
        & (Q(featured_until__isnull=True) | Q(featured_until__gte=today))
    )


def live_events():
    return (
        Event.objects.filter(status=Event.Status.LIVE)
        .prefetch_related('dates', 'ticket_types')
    )


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicEventListView(APIView):
    """GET /api/events?q=&category=&city=&locality=&featured=&page=&limit=

    Only LIVE events, newest first. Cached ~60s like /api/venues.

    Place matching (q / city / locality) goes through events/location.py:
    spellings are interchangeable (Hanamkonda = Hanumakonda) and an area
    name covers the localities inside it (Warangal ⊃ Hanamkonda, Kazipet),
    checked across venue name, address, locality and city — so an event is
    found however the organiser typed its place.

    featured=1 returns only promoted events that still have a date to sell,
    most recently promoted first — the home page's featured row.

    Location (optional, never stored) works exactly as on /api/venues:
    lat & lng add `distanceKm` to each event, sort=distance orders nearest
    first (events without a pin last), radius=<km> excludes farther events
    and events without a pin.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request):
        params = request.query_params
        try:
            limit = int(params.get('limit', DEFAULT_LIMIT))
            page = int(params.get('page', 1))
        except (TypeError, ValueError):
            return _message('page and limit must be numbers.', status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= MAX_LIMIT):
            return _message(
                f'limit must be between 1 and {MAX_LIMIT}.', status.HTTP_400_BAD_REQUEST
            )
        if page < 1:
            return _message('page must be 1 or higher.', status.HTTP_400_BAD_REQUEST)

        try:
            origin, radius = parse_location_params(params)
        except LocationParamError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)
        sort = params.get('sort', '')
        if sort and sort != 'distance':
            return _message('sort must be "distance" (or omitted).', status.HTTP_400_BAD_REQUEST)
        if sort == 'distance' and origin is None:
            return _message('sort=distance needs lat and lng.', status.HTTP_400_BAD_REQUEST)

        queryset = live_events()

        q = params.get('q', '').strip()
        if q:
            queryset = queryset.filter(search_q(q))
        if params.get('category'):
            queryset = queryset.filter(
                category__iexact=canonical_event_category(params['category'])
            )
        if params.get('city', '').strip():
            queryset = queryset.filter(place_q(params['city']))
        if params.get('locality', '').strip():
            queryset = queryset.filter(place_q(params['locality']))
        if params.get('featured') in ('1', 'true', 'yes'):
            # A promoted event whose run is over has nothing to sell; it
            # drops out of the row on its own rather than waiting for the
            # admin to notice.
            # The admin's optional schedule (featured_from / featured_until)
            # and order (featured_order) apply too — see featured_events_q.
            queryset = queryset.filter(featured_events_q()).order_by(
                *FEATURED_ORDERING
            )
        # The place filters span joins (featured) — never repeat an event.
        queryset = queryset.distinct()
        start = (page - 1) * limit

        distances = {}
        if origin is not None and (radius is not None or sort == 'distance'):
            if radius is not None:
                min_lat, max_lat, min_lng, max_lng = bounding_box(*origin, radius)
                queryset = queryset.filter(latitude__range=(min_lat, max_lat))
                if min_lng is not None:
                    queryset = queryset.filter(longitude__range=(min_lng, max_lng))
            keys, distances = rank_by_distance(
                list(queryset.values_list('pk', 'latitude', 'longitude')),
                origin, radius, nearest_first=sort == 'distance',
            )
            total = len(keys)
            page_keys = keys[start:start + limit]
            by_pk = live_events().in_bulk(page_keys)
            rows = [by_pk[key] for key in page_keys if key in by_pk]
        else:
            total = queryset.count()
            rows = list(queryset[start:start + limit])

        from bookings.ratings import event_ratings_map

        ratings = event_ratings_map([row.pk for row in rows])
        events = []
        for row in rows:
            summary = event_summary(row, ratings)
            if origin is not None:
                summary['distanceKm'] = (
                    distances[row.pk] if row.pk in distances
                    else distance_km(origin, row.latitude, row.longitude)
                )
            events.append(summary)
        return Response({
            'events': events,
            'total': total,
            'categories': event_categories_payload(),
        })


class PublicEventDetailView(APIView):
    """GET /api/events/<idOrSlug> — the full event record.

    Not cache_page'd: the payload carries live per-date BOOKABILITY, and a
    minute-old "yes, that night is available" on a nearly sold-out Dandiya
    night is exactly the lie the venue availability endpoint also refuses to
    tell. (It carries no seat counts — see date_rows.)
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request, id_or_slug):
        queryset = live_events()
        try:
            event = queryset.filter(pk=uuid.UUID(str(id_or_slug))).first()
        except ValueError:
            event = queryset.filter(slug=id_or_slug).first()
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)
        return Response(event_record(event))


class PublicEventAvailabilityView(APIView):
    """GET /api/events/<id>/availability?date=YYYY-MM-DD (public, uncached)."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request, event_id):
        event = live_events().filter(pk=event_id).first()
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)

        raw_date = request.query_params.get('date')
        if not raw_date:
            # No date given: report every sellable night in one call, which
            # is what the date picker actually wants.
            return Response({'dates': date_rows(event)})

        try:
            date = parse_date(raw_date)
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)
        if date < today_ist():
            return _message('date cannot be in the past.', status.HTTP_400_BAD_REQUEST)

        event_date = EventDate.objects.filter(event=event, date=date).first()
        if event_date is None:
            return _message(
                'This event is not running on that date.', status.HTTP_404_NOT_FOUND
            )
        return Response(availability(event, event_date))
