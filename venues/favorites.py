"""
Customer favourites (wishlist) — cross-device sync.

GET  /api/users/me/favorites — the customer's list, newest first.
PUT  /api/users/me/favorites — FULL REPLACE of the list (merge logic lives on
the client; last-write-wins is the agreed model).

Rules: cap 100 (400 above), unknown/stale venue ids dropped silently, unit
siblings fold into their base venue, duplicates deduped keeping the earliest
addedAt. Error shape {"message": "..."}.
"""
import datetime
import uuid

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Favorite, Listing

MAX_FAVORITES = 100


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _parse_added_at(text):
    try:
        parsed = datetime.datetime.fromisoformat(
            str(text or '').strip().replace('Z', '+00:00')
        )
    except ValueError:
        return timezone.now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _base_listing(listing):
    """Fold a unit sibling onto its base venue (same rule as ratings)."""
    unit_of = str((listing.record.get('detail') or {}).get('unitOf') or '').strip()
    if unit_of:
        base = Listing.objects.filter(pk=unit_of).first()
        if base is not None:
            return base
    return listing


def _payload(user):
    return {
        'favorites': [
            {'venueId': str(fav.listing_id), 'addedAt': fav.added_at.isoformat()}
            for fav in user.favorites.order_by('-added_at')
        ]
    }


class FavoritesView(APIView):
    """GET/PUT /api/users/me/favorites (User JWT)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_payload(request.user))  # [] when never saved — not 404

    def put(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        items = body.get('favorites')
        if not isinstance(items, list):
            return _message('favorites must be a list.', status.HTTP_400_BAD_REQUEST)
        if len(items) > MAX_FAVORITES:
            return _message(
                f'Maximum {MAX_FAVORITES} favourites.', status.HTTP_400_BAD_REQUEST
            )

        # Fold + dedupe: base venue id -> earliest addedAt. Stale ids are
        # dropped silently — a dead venue must never block the rest.
        folded = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = str(item.get('venueId') or '').strip()
            try:
                listing = Listing.objects.filter(pk=uuid.UUID(raw_id)).first()
            except ValueError:
                continue
            if listing is None:
                continue
            base = _base_listing(listing)
            added = _parse_added_at(item.get('addedAt'))
            key = str(base.pk)
            if key not in folded or added < folded[key][1]:
                folded[key] = (base, added)

        with transaction.atomic():
            request.user.favorites.all().delete()  # full replace
            Favorite.objects.bulk_create([
                Favorite(user=request.user, listing=base, added_at=added)
                for base, added in folded.values()
            ])

        return Response(_payload(request.user))
