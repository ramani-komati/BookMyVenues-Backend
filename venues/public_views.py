"""
Public browsing endpoints (frontend contract, Group 1).

No authentication — this is what the home page and venue detail page use.
Responses are cached for 60 seconds: the same page asked for by many
visitors hits the database once a minute instead of once per visitor.
Only LIVE listings are ever returned.
"""
import uuid

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Listing

CACHE_SECONDS = 60
MAX_LIMIT = 50
DEFAULT_LIMIT = 20

SORTS = {
    # No ratings/booking counts yet — "popular" falls back to recently
    # updated. Swap for a real popularity metric once bookings exist.
    'popular': '-updated_at',
    'new': '-created_at',
}


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _summary(listing):
    """List row: the record without the heavy gallery/detail blocks."""
    summary = {
        key: value
        for key, value in listing.record.items()
        if key not in ('gallery', 'detail')
    }
    summary['id'] = str(listing.id)
    summary['status'] = listing.status
    summary['slug'] = listing.slug
    return summary


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicVenueListView(APIView):
    """GET /api/venues?q=&category=&locality=&pincode=&page=&limit=&sort="""

    permission_classes = [AllowAny]

    def get(self, request):
        params = request.query_params

        try:
            limit = int(params.get('limit', DEFAULT_LIMIT))
            page = int(params.get('page', 1))
        except ValueError:
            return _message('page and limit must be numbers.', status.HTTP_400_BAD_REQUEST)

        if not (1 <= limit <= MAX_LIMIT):
            return _message(f'limit must be between 1 and {MAX_LIMIT}.', status.HTTP_400_BAD_REQUEST)
        if page < 1:
            return _message('page must be 1 or higher.', status.HTTP_400_BAD_REQUEST)

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
        offset = (page - 1) * limit
        rows = queryset[offset:offset + limit]

        return Response({'venues': [_summary(row) for row in rows], 'total': total})


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicVenueDetailView(APIView):
    """GET /api/venues/<idOrSlug> — full record incl. gallery + detail."""

    permission_classes = [AllowAny]

    def get(self, request, id_or_slug):
        queryset = Listing.objects.filter(status=Listing.Status.LIVE)

        try:
            listing = queryset.filter(pk=uuid.UUID(id_or_slug)).first()
        except ValueError:
            listing = queryset.filter(slug=id_or_slug).first()

        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        record = dict(listing.record)
        record['slug'] = listing.slug
        return Response(record)
