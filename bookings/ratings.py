"""
Venue ratings — POST /api/venues/:venueId/ratings (customer JWT).

A rating belongs to one COMPLETED booking (one rating per booking, enforced by
the DB). The aggregate is computed over the venue SET — the base listing plus
its "— Pitch/Screen N" unit siblings — because they are one physical venue.
Error shape: {"message": "..."} (customer app conventions).
"""
from django.db.models import Avg, Count
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.models import Listing

from .models import Booking, Rating
from .slots import now_minutes_ist, slots_end_minute, today_ist
from .views import _message


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


def venue_rating(listing):
    """(average rounded to 4 decimals or None, count) over the venue set."""
    result = Rating.objects.filter(
        listing_id__in=venue_set_ids(listing)
    ).aggregate(avg=Avg('stars'), count=Count('id'))
    if not result['count']:
        return None, 0
    return round(result['avg'], 4), result['count']


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
    """GET /api/vendors/me/ratings?limit=&offset=&venueId= — the individual
    ratings behind the vendor's averages, newest first."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from accounts.models import User
        if request.user.role != User.Role.VENDOR:
            return _message(
                'Only vendor accounts can access this endpoint.',
                status.HTTP_403_FORBIDDEN,
            )

        queryset = (
            Rating.objects.filter(listing__vendor=request.user)
            .select_related('listing', 'user', 'booking')
            .order_by('-created_at')
        )

        venue_id = str(request.query_params.get('venueId') or '').strip()
        if venue_id:
            target = None
            try:
                import uuid as uuid_mod
                target = Listing.objects.filter(
                    pk=uuid_mod.UUID(venue_id), vendor=request.user
                ).first()
            except ValueError:
                pass
            if target is None:
                return _message('Venue not found.', status.HTTP_404_NOT_FOUND)
            queryset = queryset.filter(listing_id__in=venue_set_ids(target))

        try:
            limit = int(request.query_params.get('limit', 20))
            offset = int(request.query_params.get('offset', 0))
        except (TypeError, ValueError):
            return _message('limit and offset must be numbers.',
                            status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= 50) or offset < 0:
            return _message('Invalid limit or offset.', status.HTTP_400_BAD_REQUEST)

        total = queryset.count()
        rows = queryset[offset:offset + limit]
        return Response({
            'ratings': [
                {
                    'id': str(rating.id),
                    'venueId': str(rating.listing_id),
                    'venueName': rating.listing.name or rating.booking.venue_name,
                    'unitLabel': rating.booking.unit_label or None,
                    'stars': rating.stars,
                    'customerName': _display_name(rating.user),
                    'bookingDate': rating.booking.date.isoformat(),
                    'createdAt': rating.created_at.isoformat(),
                }
                for rating in rows
            ],
            'total': total,
        })


class RateVenueView(APIView):
    """POST /api/venues/<venueId>/ratings {bookingId, stars} -> aggregate."""

    permission_classes = [IsAuthenticated]

    def post(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id).first()
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        body = request.data if isinstance(request.data, dict) else {}

        try:
            stars = int(str(body.get('stars')))
        except (TypeError, ValueError):
            stars = 0
        if not 1 <= stars <= 5:
            return _message('stars must be a whole number from 1 to 5.',
                            status.HTTP_400_BAD_REQUEST)

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

        if Rating.objects.filter(booking=booking).exists():
            return _message('Already rated', status.HTTP_409_CONFLICT)

        Rating.objects.create(
            booking=booking, listing=listing, user=request.user, stars=stars
        )

        average, count = venue_rating(listing)
        return Response({'rating': average, 'count': count},
                        status=status.HTTP_201_CREATED)
