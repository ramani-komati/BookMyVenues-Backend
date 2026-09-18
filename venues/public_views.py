"""
Public browsing endpoints (frontend contract, Group 1).

No authentication — this is what the home page and venue detail page use.
Responses are cached for 60 seconds: the same page asked for by many
visitors hits the database once a minute instead of once per visitor.
Only LIVE listings are ever returned.
"""
import uuid

from django.utils.decorators import method_decorator
from django.core.cache import cache
from django.views.decorators.cache import cache_page
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Listing

CACHE_SECONDS = 60
MAX_LIMIT = 50
DEFAULT_LIMIT = 20
# How long a venue wears the "New" badge (also exposed in GET /api/config).
NEW_BADGE_DAYS = 15

SORTS = {
    # No ratings/booking counts yet — "popular" falls back to recently
    # updated. Swap for a real popularity metric once bookings exist.
    'popular': '-updated_at',
    'new': '-created_at',
}


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _derive_meta(listing):
    """meta is SERVER-DERIVED at read time — 'New' while submittedAt is within
    the last NEW_BADGE_DAYS, '' after. The client's stored meta string is
    ignored (stale values like 'Under review' can never leak again)."""
    import datetime

    from django.utils import timezone

    raw = str((listing.record or {}).get('submittedAt') or '').strip()
    submitted = None
    if raw:
        try:
            submitted = datetime.datetime.fromisoformat(raw.replace('Z', '+00:00'))
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            submitted = None
    if submitted is None:
        submitted = listing.created_at
    age = timezone.now() - submitted
    return 'New' if age.days < NEW_BADGE_DAYS else ''


def _summary(listing, ratings=None):
    """List row: the record minus the heavy `detail` block. Keeps `gallery`
    (P3) so venue cards can show the photo slideshow without a call per venue.

    `ratings` is the precomputed {listing_id: (avg, count)} map from
    venue_ratings_map — pass it when rendering a LIST, or every row costs two
    extra queries."""
    summary = {
        key: value
        for key, value in listing.record.items()
        if key != 'detail'
    }
    from .taxonomy import canonical_category, sub_categories_for

    summary['id'] = str(listing.id)
    summary['status'] = listing.status
    summary['slug'] = listing.slug
    # Canonical taxonomy so the home nav can filter reliably: legacy category
    # spellings are aliased ('Private Hall'/'Box cricket' -> 'Party hall'/
    # 'Play zone') and sub-categories are flattened from wherever the vendor
    # stored them (subCategories / occasions / sports).
    summary['category'] = canonical_category(
        summary.get('category') or listing.category
    )
    summary['subCategories'] = sub_categories_for(listing)
    summary['meta'] = _derive_meta(listing)  # server-derived, never the stored string
    summary.setdefault('gallery', [])  # always present, even if the vendor added none
    if ratings is not None:
        average, count = ratings.get(str(listing.id), (None, 0))
    else:
        from bookings.ratings import venue_rating  # avoid import cycle at load
        average, count = venue_rating(listing)
    if average is not None:  # server rating takes precedence on the frontend
        summary['rating'] = average
        summary['ratingCount'] = count
    return summary


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicVenueListView(APIView):
    # Free-text q/locality/pincode run icontains scans, and varying the query
    # by one character defeats the 60s response cache.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    """GET /api/venues?q=&category=&locality=&pincode=&page=&limit=&sort="""

    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params

        try:
            limit = int(params.get('limit', DEFAULT_LIMIT))
            page = int(params.get('page', 1))
            # offset is an alternative to page (0-based row start). It is
            # HONOURED, never silently ignored — silent ignoring hides bugs.
            offset = int(params['offset']) if 'offset' in params else None
        except (ValueError, TypeError):
            return _message(
                'page, limit and offset must be numbers.', status.HTTP_400_BAD_REQUEST
            )

        if not (1 <= limit <= MAX_LIMIT):
            return _message(f'limit must be between 1 and {MAX_LIMIT}.', status.HTTP_400_BAD_REQUEST)
        if page < 1:
            return _message('page must be 1 or higher.', status.HTTP_400_BAD_REQUEST)
        if offset is not None and offset < 0:
            return _message('offset must be 0 or higher.', status.HTTP_400_BAD_REQUEST)

        sort = params.get('sort', 'new')
        if sort not in SORTS:
            return _message('sort must be "popular" or "new".', status.HTTP_400_BAD_REQUEST)

        queryset = Listing.objects.filter(status=Listing.Status.LIVE)

        q = params.get('q', '').strip()
        if q:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(name__icontains=q) | Q(locality__icontains=q) | Q(category__icontains=q)
            )
        if params.get('category'):
            queryset = queryset.filter(category__iexact=params['category'])
        if params.get('locality'):
            queryset = queryset.filter(locality__icontains=params['locality'])
        if params.get('pincode'):
            queryset = queryset.filter(pincode=params['pincode'])

        queryset = queryset.order_by(SORTS[sort])

        total = queryset.count()
        start = offset if offset is not None else (page - 1) * limit
        rows = list(queryset[start:start + limit])

        from bookings.ratings import venue_ratings_map
        ratings = venue_ratings_map(rows)   # two queries for the whole page
        return Response({
            'venues': [_summary(row, ratings) for row in rows],
            'total': total,
        })


def detail_cache_keys(listing):
    """Every cache key the venue detail can be served under.

    The endpoint accepts an id OR a slug, so both spellings are cached and
    both must be dropped when the venue changes.
    """
    return [f'venue:detail:{listing.pk}', f'venue:detail:{listing.slug}']


def invalidate_listing_cache(listing):
    """Drop a venue's cached detail so an edit is visible immediately.

    Called on republish and on admin status changes. Without this a vendor
    who deletes an offer keeps seeing it for up to CACHE_SECONDS and
    reasonably concludes the save did not work.
    """
    cache.delete_many(detail_cache_keys(listing))


class PublicVenueDetailView(APIView):
    """GET /api/venues/<idOrSlug> — full record incl. gallery + detail.

    Cached by hand rather than with cache_page so a republish can invalidate
    exactly this venue — cache_page keys on the whole request and cannot be
    cleared for one listing without flushing the shared cache, which would
    also reset the throttle counters that live there.
    """

    permission_classes = [AllowAny]

    def get(self, request, id_or_slug):
        cache_key = f'venue:detail:{id_or_slug}'
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        queryset = Listing.objects.filter(status=Listing.Status.LIVE)

        try:
            listing = queryset.filter(pk=uuid.UUID(id_or_slug)).first()
        except ValueError:
            listing = queryset.filter(slug=id_or_slug).first()

        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        from bookings.ratings import venue_rating
        from .taxonomy import canonical_category, sub_categories_for
        record = dict(listing.record)
        record['slug'] = listing.slug
        record['meta'] = _derive_meta(listing)
        record['category'] = canonical_category(
            record.get('category') or listing.category
        )
        record['subCategories'] = sub_categories_for(listing)
        average, count = venue_rating(listing)
        if average is not None:
            record['rating'] = average
            record['ratingCount'] = count

        # Part payment, served in BOTH places the contract names: at the top
        # level where the admin panel writes it, and inside `detail` where the
        # customer booking screen reads it. Normalised through read_config, so
        # a malformed venue config reads back as null (= pay in full) rather
        # than a shape the client has to defend against.
        from bookings.part_payment import read_config as read_part_payment
        part = read_part_payment(listing.record)
        record['partPayment'] = part
        # Copy before writing: `detail` is still the dict loaded from the row.
        detail = dict(record.get('detail') or {})
        detail['partPayment'] = part
        record['detail'] = detail

        cache.set(cache_key, record, CACHE_SECONDS)
        return Response(record)
