"""
Ratings & reviews — one rating (1–5 stars + an optional written review) per
completed, verified-paid booking or event ticket.

    POST /api/venues/:venueId/ratings   {bookingId, stars, review?}  (customer JWT)
    POST /api/events/:eventId/ratings   {orderId, stars, review?}    (customer JWT)
    GET  /api/venues/:venueId/reviews   written reviews, public
    GET  /api/events/:eventId/reviews   written reviews, public

A rating belongs to one COMPLETED booking (one rating per booking, enforced by
the DB). The venue aggregate is computed over the venue SET — the base listing
plus its "— Pitch/Screen N" unit siblings — because they are one physical
venue. An event's aggregate is over every ticket order for that event.

ELIGIBILITY (the server decides; the customer's clock never does):
  * the signed-in customer owns the booking / order;
  * it is CONFIRMED with a verified Razorpay payment — cancelled, unpaid,
    failed, refund-pending and refunded rows can never be rated, and neither
    can walk-ins or the retired pay-at-venue bookings that never went through
    the gateway;
  * it has actually happened: a venue booking's last slot has ended (IST),
    an event ticket's show has ended (IST, overnight shows included).
A rating hidden by the super admin (moderation) stays stored but leaves every
average, count and public review list.

Error shape: {"message": "..."} (customer app conventions).
"""
import datetime
import uuid as uuid_mod

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from venues.models import Listing

from .models import Booking, Rating
from .slots import IST, now_minutes_ist, slots_end_minute, today_ist
from .views import _message

# ratingStatus values on the customer's booking / ticket history.
ELIGIBLE = 'eligible'            # finished, paid, not rated yet -> ask
RATED = 'rated'                  # already rated (one per booking)
NOT_COMPLETED = 'not_completed'  # paid, but the slot / show hasn't ended
NOT_ELIGIBLE = 'not_eligible'    # not a paid, confirmed customer booking

# Public review lists page size.
REVIEWS_PAGE = 10
REVIEWS_MAX_PAGE = 20


def visible_ratings():
    """Ratings that count: everything except what moderation hid."""
    return Rating.objects.filter(hidden_at__isnull=True)


def _base_id(listing):
    """The venue set's base id: the listing's unitOf target, or itself."""
    unit_of = str((listing.record.get('detail') or {}).get('unitOf') or '').strip()
    return unit_of or str(listing.id)


def venue_set_ids(listing):
    """All listing ids of the physical venue: base + its unit siblings."""
    base = _base_id(listing)
    sibling_ids = Listing.objects.filter(
        record__detail__unitOf=base
    ).values_list('id', flat=True)
    return {base} | {str(pk) for pk in sibling_ids}


def venue_set_listings(listing):
    """The venue set's Listing rows (a malformed stored unitOf is skipped)."""
    ids = []
    for pk in venue_set_ids(listing):
        try:
            ids.append(uuid_mod.UUID(str(pk)))
        except ValueError:
            continue
    return Listing.objects.filter(pk__in=ids)


def venue_rating(listing):
    """(average rounded to 4 decimals or None, count) over the venue set.

    Single-listing use only (venue detail page). For a LIST of venues use
    venue_ratings_map — this costs two queries per venue."""
    result = visible_ratings().filter(
        listing_id__in=venue_set_ids(listing)
    ).aggregate(avg=Avg('stars'), count=Count('id'))
    if not result['count']:
        return None, 0
    return round(result['avg'], 4), result['count']


def venue_ratings_map(listings):
    """
    {listing_id: (average, count)} for a whole page of venues in ONE query,
    folded per venue set (a rating on "— Pitch 2" counts for its base).

    Called once per list response; the per-venue venue_rating() would cost two
    queries EACH, which is what made the venue list slow.
    """
    listings = list(listings)
    if not listings:
        return {}

    base_of = {str(l.id): _base_id(l) for l in listings}
    bases = set(base_of.values())

    # Ratings on the base venues themselves OR on any of their unit siblings,
    # fetched with each rated listing's record so we can fold without a second
    # round trip.
    rows = visible_ratings().filter(
        Q(listing_id__in=list(bases))
        | Q(listing__record__detail__unitOf__in=list(bases))
    ).values_list('listing_id', 'stars', 'listing__record')

    totals = {}
    for listing_id, stars, record in rows:
        unit_of = str(((record or {}).get('detail') or {}).get('unitOf') or '').strip()
        base = unit_of or str(listing_id)
        if base in bases:
            entry = totals.setdefault(base, [0, 0])
            entry[0] += stars
            entry[1] += 1

    return {
        listing_id: (
            (round(totals[base][0] / totals[base][1], 4), totals[base][1])
            if base in totals else (None, 0)
        )
        for listing_id, base in base_of.items()
    }


def event_rating(event_id):
    """(average rounded to 4 decimals or None, count) for one event."""
    result = visible_ratings().filter(event_id=event_id).aggregate(
        avg=Avg('stars'), count=Count('id'),
    )
    if not result['count']:
        return None, 0
    return round(result['avg'], 4), result['count']


def event_ratings_map(event_ids):
    """{str(event_id): (average, count)} for many events in ONE query —
    the event-card counterpart of venue_ratings_map. Unrated events map to
    (None, 0)."""
    ids = [str(pk) for pk in event_ids]
    if not ids:
        return {}
    rows = (
        visible_ratings().filter(event_id__in=ids)
        .values('event_id').annotate(avg=Avg('stars'), count=Count('id'))
    )
    found = {
        str(row['event_id']): (round(row['avg'], 4), row['count']) for row in rows
    }
    return {pk: found.get(pk, (None, 0)) for pk in ids}


def _booking_completed(booking):
    """Same rule as the cancellation guard: past date, or today with the
    last slot's end time already passed (IST)."""
    today = today_ist()
    if booking.date < today:
        return True
    if booking.date == today:
        end = slots_end_minute(booking.slots)
        return bool(end) and end <= now_minutes_ist()
    return False


def order_show_ended(order, now=None):
    """Has the show this ticket is for finished (IST)?

    The show ends on the order's date at its end time; 00:00 as an end means
    midnight, and an end time at or before the start time means the show runs
    past midnight and ends the NEXT day. No end time on record -> the end of
    the date. Stricter than EventOrder.display_status on overnight shows on
    purpose: a review must never be asked for while the show is still on."""
    now = now or datetime.datetime.now(IST)
    end_day = order.date
    end_time = order.end_time
    if end_time is None or (end_time.hour == 0 and end_time.minute == 0):
        end_day = order.date + datetime.timedelta(days=1)
        end_time = datetime.time(0, 0)
    elif order.start_time is not None and end_time <= order.start_time:
        end_day = order.date + datetime.timedelta(days=1)
    ends_at = datetime.datetime.combine(end_day, end_time, tzinfo=IST)
    return ends_at <= now


def _verified_paid(row):
    """Confirmed AND the gateway confirmed the money (a Razorpay payment id).
    Refunds, cancellations and failed or abandoned checkouts all fail this."""
    return row.status == 'confirmed' and bool(row.razorpay_payment_id)


def _has_rating(row):
    # A missing reverse one-to-one raises (an AttributeError subclass);
    # getattr's default covers it.
    return getattr(row, 'rating', None) is not None


def booking_rating_status(booking):
    """ELIGIBLE | RATED | NOT_COMPLETED | NOT_ELIGIBLE for a venue booking.
    Costs no query when the caller select_related('rating')."""
    if _has_rating(booking):
        return RATED
    if booking.walk_in or booking.user_id is None or not _verified_paid(booking):
        return NOT_ELIGIBLE
    if not _booking_completed(booking):
        return NOT_COMPLETED
    return ELIGIBLE


def order_rating_status(order):
    """ELIGIBLE | RATED | NOT_COMPLETED | NOT_ELIGIBLE for an event ticket."""
    if _has_rating(order):
        return RATED
    if order.user_id is None or order.event_id is None or not _verified_paid(order):
        return NOT_ELIGIBLE
    if not order_show_ended(order):
        return NOT_COMPLETED
    return ELIGIBLE


# What the customer is told when a rating is refused for the booking's state.
# Each says what is actually wrong with THIS booking.
_STATE_MESSAGES = {
    'cancelled': 'Cancelled bookings can’t be rated.',
    'refunded': 'Refunded bookings can’t be rated.',
    'refund_pending': 'Bookings being refunded can’t be rated.',
    'failed': 'Only paid bookings can be rated.',
    'payment_pending': 'Only paid bookings can be rated.',
    'removed': 'Only paid bookings can be rated.',
}


def _ineligible(row):
    """409 for a booking/order that is not a verified-paid customer one."""
    message = _STATE_MESSAGES.get(row.status, 'Only paid bookings can be rated.')
    return Response(
        {'message': message, 'code': 'NOT_ELIGIBLE'},
        status=status.HTTP_409_CONFLICT,
    )


def _parse_stars(body):
    try:
        stars = int(str(body.get('stars')))
    except (TypeError, ValueError):
        return None
    return stars if 1 <= stars <= 5 else None


def _parse_review(body):
    """(text, error). Optional; trimmed; at most Rating.MAX_REVIEW_LENGTH."""
    raw = body.get('review')
    if raw is None:
        return '', None
    if not isinstance(raw, str):
        return '', 'review must be text.'
    text = raw.strip()
    if len(text) > Rating.MAX_REVIEW_LENGTH:
        return '', f'Reviews can be at most {Rating.MAX_REVIEW_LENGTH} characters.'
    return text, None


def ratings_changed(listings=(), event_id=None):
    """After a rating is added, hidden or restored: drop the cached venue
    detail pages it shows on and invalidate the public homepage everywhere.
    (The venue and event LISTS are cache_page'd for 60 s and refresh on
    their own.)"""
    from adminpanel.models import Settings
    from venues.public_views import invalidate_listing_cache

    for listing in listings:
        invalidate_listing_cache(listing)
    Settings.bump_public_version()


def _display_name(user):
    """Display-safe customer name: 'Ravi Kumar' -> 'Ravi K.'. Never the
    phone/email; 'Customer' when no name is stored."""
    name = (getattr(user, 'name', '') or '').strip()
    if not name:
        return 'Customer'
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    return f'{parts[0]} {parts[1][0].upper()}.'


class VendorRatingsView(APIView):
    """GET /api/vendors/me/ratings?limit=&offset=&venueId=&eventId=&kind=
    &stars=&from=&to= — the individual ratings behind the vendor's averages,
    newest first: their venues' AND their own events'. Read-only on the
    customer's side; the vendor's only write is their reply
    (VendorRatingReplyView)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not request.user.has_vendor_access:
            return _message(
                'Only vendor accounts can access this endpoint.',
                status.HTTP_403_FORBIDDEN,
            )

        params = request.query_params
        kind = str(params.get('kind') or 'all')
        if kind not in ('all', 'venue', 'event'):
            return _message('kind must be all, venue or event.', status.HTTP_400_BAD_REQUEST)
        mine = Q(listing__vendor=request.user) | Q(event__vendor=request.user)
        if kind == 'venue':
            mine = Q(listing__vendor=request.user)
        elif kind == 'event':
            mine = Q(event__vendor=request.user)
        queryset = (
            visible_ratings().filter(mine)
            .select_related('listing', 'user', 'booking', 'event', 'event_order')
            # `-id` is the tiebreaker, not decoration: two ratings submitted
            # in the same clock tick (auto_now_add has no sub-tick ordering)
            # otherwise come back in an arbitrary order, so paging over them
            # could show or skip the same row twice.
            .order_by('-created_at', '-id')
        )

        venue_id = str(params.get('venueId') or '').strip()
        if venue_id:
            target = None
            try:
                target = Listing.objects.filter(
                    pk=uuid_mod.UUID(venue_id), vendor=request.user
                ).first()
            except ValueError:
                pass
            if target is None:
                return _message('Venue not found.', status.HTTP_404_NOT_FOUND)
            queryset = queryset.filter(listing_id__in=venue_set_ids(target))

        event_id = str(params.get('eventId') or '').strip()
        if event_id:
            try:
                queryset = queryset.filter(
                    event_id=uuid_mod.UUID(event_id), event__vendor=request.user,
                )
            except ValueError:
                return _message('Event not found.', status.HTTP_404_NOT_FOUND)

        stars = str(params.get('stars') or '').strip()
        if stars:
            if stars not in ('1', '2', '3', '4', '5'):
                return _message('stars must be 1 to 5.', status.HTTP_400_BAD_REQUEST)
            queryset = queryset.filter(stars=int(stars))
        for name, lookup in (('from', 'created_at__date__gte'), ('to', 'created_at__date__lte')):
            raw = str(params.get(name) or '').strip()
            if raw:
                try:
                    queryset = queryset.filter(**{lookup: datetime.date.fromisoformat(raw)})
                except ValueError:
                    return _message(f'{name} must be a date (YYYY-MM-DD).',
                                    status.HTTP_400_BAD_REQUEST)

        try:
            limit = int(params.get('limit', 20))
            offset = int(params.get('offset', 0))
        except (TypeError, ValueError):
            return _message('limit and offset must be numbers.',
                            status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= 50) or offset < 0:
            return _message('Invalid limit or offset.', status.HTTP_400_BAD_REQUEST)

        total = queryset.count()
        rows = queryset[offset:offset + limit]
        return Response({
            'ratings': [vendor_rating_row(rating) for rating in rows],
            'total': total,
        })


def vendor_rating_row(rating):
    """One rating as its venue/event owner sees it. The customer is a
    display name only (never a phone or email)."""
    is_event = rating.event_order_id is not None
    if is_event:
        order = rating.event_order
        target_name = (rating.event.name if rating.event_id and rating.event else '') or \
            (order.event_name if order else '')
        booking_date = order.date.isoformat() if order else None
    else:
        target_name = (rating.listing.name if rating.listing_id and rating.listing else '') or \
            (rating.booking.venue_name if rating.booking_id else '')
        booking_date = rating.booking.date.isoformat() if rating.booking_id else None
    return {
        'id': str(rating.id),
        'kind': 'event' if is_event else 'venue',
        'venueId': str(rating.listing_id) if rating.listing_id else None,
        'venueName': None if is_event else target_name,
        'eventId': str(rating.event_id) if rating.event_id else None,
        'eventName': target_name if is_event else None,
        'unitLabel': (rating.booking.unit_label or None) if rating.booking_id else None,
        'stars': rating.stars,
        # The customer's written words, when they left any.
        'review': rating.review or None,
        'customerName': _display_name(rating.user),
        'bookingDate': booking_date,
        'createdAt': rating.created_at.isoformat(),
        'reply': rating.reply or None,
        'repliedAt': rating.replied_at.isoformat() if rating.replied_at else None,
        'replyHidden': rating.reply_hidden_at is not None,
        'replyHiddenReason': rating.reply_hidden_reason or None,
    }


class VendorRatingReplyView(APIView):
    """PUT    /api/vendors/me/ratings/<id>/reply {reply} — write / edit the
           public reply to a review of the vendor's own venue or event.
    DELETE /api/vendors/me/ratings/<id>/reply — remove it.

    The customer's stars and words are never editable here. A reply the
    super admin hid can be removed but not rewritten (a rewrite must not
    slip past moderation)."""

    permission_classes = [IsAuthenticated]

    def _rating(self, request, rating_id):
        if not request.user.has_vendor_access:
            return None, _message('Only vendor accounts can access this endpoint.',
                                  status.HTTP_403_FORBIDDEN)
        rating = visible_ratings().filter(
            Q(listing__vendor=request.user) | Q(event__vendor=request.user), pk=rating_id,
        ).select_related('listing', 'user', 'booking', 'event', 'event_order').first()
        if rating is None:
            return None, _message('Review not found.', status.HTTP_404_NOT_FOUND)
        return rating, None

    def put(self, request, rating_id):
        from django.utils import timezone

        rating, error = self._rating(request, rating_id)
        if error:
            return error
        if rating.reply_hidden_at is not None:
            reason = f' ({rating.reply_hidden_reason})' if rating.reply_hidden_reason else ''
            return _message(
                f'Your reply was hidden by the TheBookMyVenues team{reason}. '
                'You can delete it and contact support.',
                status.HTTP_409_CONFLICT,
            )
        body = request.data if isinstance(request.data, dict) else {}
        reply = str(body.get('reply') or '').strip()
        if not reply:
            return _message('Write a reply first.', status.HTTP_400_BAD_REQUEST)
        if len(reply) > Rating.MAX_REPLY_LENGTH:
            return _message(f'A reply can be at most {Rating.MAX_REPLY_LENGTH} characters.',
                            status.HTTP_400_BAD_REQUEST)
        rating.reply = reply
        rating.replied_at = timezone.now()
        rating.save(update_fields=['reply', 'replied_at', 'updated_at'])
        return Response({'rating': vendor_rating_row(rating)})

    def delete(self, request, rating_id):
        rating, error = self._rating(request, rating_id)
        if error:
            return error
        rating.reply = ''
        rating.replied_at = None
        rating.reply_hidden_at = None
        rating.reply_hidden_reason = ''
        rating.save(update_fields=['reply', 'replied_at', 'reply_hidden_at',
                                   'reply_hidden_reason', 'updated_at'])
        return Response({'rating': vendor_rating_row(rating)})


class RateVenueView(APIView):
    """POST /api/venues/<venueId>/ratings {bookingId, stars, review?} -> aggregate."""

    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id).first()
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}

        stars = _parse_stars(body)
        if stars is None:
            return _message('stars must be a whole number from 1 to 5.',
                            status.HTTP_400_BAD_REQUEST)
        review, error = _parse_review(body)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)

        booking = Booking.objects.filter(pk=str(body.get('bookingId') or '')).first()
        if booking is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)
        if booking.user_id != request.user.id:
            return _message('This booking is not yours.', status.HTTP_403_FORBIDDEN)

        # The booking must be for this venue (or a listing in the same set).
        if booking.listing_id is None or str(booking.listing_id) not in venue_set_ids(listing):
            return _message('This booking is for a different venue.',
                            status.HTTP_400_BAD_REQUEST)

        if not _booking_completed(booking):
            return _message('Booking not completed yet', status.HTTP_409_CONFLICT)

        # Only a booking the customer actually paid for and kept.
        if booking.walk_in or not _verified_paid(booking):
            return _ineligible(booking)

        if Rating.objects.filter(booking=booking).exists():
            return _message('Already rated', status.HTTP_409_CONFLICT)

        try:
            with transaction.atomic():
                Rating.objects.create(
                    booking=booking, listing=listing, user=request.user,
                    stars=stars, review=review,
                )
        except IntegrityError:
            # A double-tap that raced past the exists() check: the one-to-one
            # on booking let exactly one of them in.
            return _message('Already rated', status.HTTP_409_CONFLICT)

        ratings_changed(venue_set_listings(listing))
        average, count = venue_rating(listing)
        return Response({'rating': average, 'count': count},
                        status=status.HTTP_201_CREATED)


class RateEventView(APIView):
    """POST /api/events/<eventId>/ratings {orderId, stars, review?} -> aggregate.

    The event counterpart of RateVenueView: one rating per verified-paid
    ticket order, accepted only once the show has ended."""

    permission_classes = [IsAuthenticated]

    def post(self, request, event_id):
        from events.models import Event, EventOrder

        event = Event.objects.filter(pk=event_id).first()
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}
        stars = _parse_stars(body)
        if stars is None:
            return _message('stars must be a whole number from 1 to 5.',
                            status.HTTP_400_BAD_REQUEST)
        review, error = _parse_review(body)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)

        order = EventOrder.objects.filter(pk=str(body.get('orderId') or '')).first()
        if order is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)
        if order.user_id != request.user.id:
            return _message('This booking is not yours.', status.HTTP_403_FORBIDDEN)
        if order.event_id != event.pk:
            return _message('This booking is for a different event.',
                            status.HTTP_400_BAD_REQUEST)

        if not order_show_ended(order):
            return _message('Event not completed yet', status.HTTP_409_CONFLICT)
        if not _verified_paid(order):
            return _ineligible(order)

        if Rating.objects.filter(event_order=order).exists():
            return _message('Already rated', status.HTTP_409_CONFLICT)

        try:
            with transaction.atomic():
                Rating.objects.create(
                    event_order=order, event=event, user=request.user,
                    stars=stars, review=review,
                )
        except IntegrityError:
            return _message('Already rated', status.HTTP_409_CONFLICT)

        ratings_changed(event_id=event.pk)
        average, count = event_rating(event.pk)
        return Response({'rating': average, 'count': count},
                        status=status.HTTP_201_CREATED)


def _page(request):
    """(limit, offset) for the public review lists, or a 400 Response."""
    try:
        limit = int(request.query_params.get('limit', REVIEWS_PAGE))
        offset = int(request.query_params.get('offset', 0))
    except (TypeError, ValueError):
        return None, _message('limit and offset must be numbers.',
                              status.HTTP_400_BAD_REQUEST)
    if not (1 <= limit <= REVIEWS_MAX_PAGE) or offset < 0:
        return None, _message('Invalid limit or offset.', status.HTTP_400_BAD_REQUEST)
    return (limit, offset), None


def review_rows(queryset, limit, offset):
    """(rows, total) of WRITTEN reviews, newest first. Stars-only ratings
    count in the average but have nothing to read, so they are not listed.
    Display-safe names only — never a phone number or email."""
    written = queryset.exclude(review='').select_related('user', 'booking')
    written = written.order_by('-created_at', '-id')
    total = written.count()
    rows = [
        {
            'id': str(rating.id),
            'stars': rating.stars,
            'review': rating.review,
            'customerName': _display_name(rating.user),
            'unitLabel': (rating.booking.unit_label or None) if rating.booking_id else None,
            'createdAt': rating.created_at.isoformat(),
            # The owner's public reply, unless moderation hid it.
            'reply': rating.public_reply,
        }
        for rating in written[offset:offset + limit]
    ]
    return rows, total


class PublicVenueReviewsView(APIView):
    """GET /api/venues/<venueId>/reviews?limit=&offset= — the venue set's
    written reviews plus its aggregate. Public; LIVE venues only."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id, status=Listing.Status.LIVE).first()
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)
        page, error = _page(request)
        if error:
            return error
        queryset = visible_ratings().filter(listing_id__in=venue_set_ids(listing))
        rows, total = review_rows(queryset, *page)
        average, count = venue_rating(listing)
        return Response({'reviews': rows, 'total': total, 'rating': average, 'count': count})


class PublicEventReviewsView(APIView):
    """GET /api/events/<eventId>/reviews?limit=&offset= — an event's written
    reviews plus its aggregate. Public; LIVE events only."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request, event_id):
        from events.models import Event

        if not Event.objects.filter(pk=event_id, status=Event.Status.LIVE).exists():
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)
        page, error = _page(request)
        if error:
            return error
        rows, total = review_rows(visible_ratings().filter(event_id=event_id), *page)
        average, count = event_rating(event_id)
        return Response({'reviews': rows, 'total': total, 'rating': average, 'count': count})
