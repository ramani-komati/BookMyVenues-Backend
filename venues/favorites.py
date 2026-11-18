"""
Customer favourites (wishlist) — cross-device sync.

GET    /api/users/me/favorites — the customer's list, newest first.
POST   /api/users/me/favorites — ADD one venue ({venueId, addedAt?}).
DELETE /api/users/me/favorites/<venueId> — REMOVE one venue.
PUT    /api/users/me/favorites — FULL REPLACE (kept for older clients).

Clients change the list ONLY through POST/DELETE, i.e. only when the customer
taps a heart. A full replace sent by a page load raced other devices and could
wipe saved favourites (QA audit 2026-10-30), so PUT now refuses to empty a
non-empty list unless the body says {"confirmClear": true}.

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


def _listing_for(raw_id):
    """The base listing for a client venue id, or None (junk/unknown id)."""
    try:
        listing = Listing.objects.filter(pk=uuid.UUID(str(raw_id or '').strip())).first()
    except ValueError:
        return None
    return _base_listing(listing) if listing is not None else None


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

    def post(self, request):
        """Add ONE favourite — idempotent (re-adding keeps the first addedAt)."""
        body = request.data if isinstance(request.data, dict) else {}
        base = _listing_for(body.get('venueId'))
        if base is None:
            return _message('That venue is no longer available.', status.HTTP_404_NOT_FOUND)
        with transaction.atomic():
            existing = request.user.favorites.filter(listing=base).first()
            if existing is None:
                if request.user.favorites.count() >= MAX_FAVORITES:
                    return _message(
                        f'Maximum {MAX_FAVORITES} favourites.', status.HTTP_400_BAD_REQUEST
                    )
                Favorite.objects.create(
                    user=request.user, listing=base,
                    added_at=_parse_added_at(body.get('addedAt')),
                )
        return Response(_payload(request.user))

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
            base = _listing_for(item.get('venueId'))
            if base is None:
                continue
            added = _parse_added_at(item.get('addedAt'))
            key = str(base.pk)
            if key not in folded or added < folded[key][1]:
                folded[key] = (base, added)

        # An empty replacement of a non-empty list is what the old page-load
        # sync sent when it raced another device — never honour it silently.
        if (
            not folded
            and body.get('confirmClear') is not True
            and request.user.favorites.exists()
        ):
            return Response(
                {
                    'message': 'Refusing to clear saved favourites without confirmClear.',
                    'code': 'CLEAR_NOT_CONFIRMED',
                    **_payload(request.user),
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            request.user.favorites.all().delete()  # full replace
            Favorite.objects.bulk_create([
                Favorite(user=request.user, listing=base, added_at=added)
                for base, added in folded.values()
            ])

        return Response(_payload(request.user))


class FavoriteItemView(APIView):
    """DELETE /api/users/me/favorites/<venueId> (User JWT) — remove ONE venue.

    Idempotent: removing a venue that isn't saved (or no longer exists) still
    answers 200 with the list, so a retried removal never errors."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, venue_id):
        base = _listing_for(venue_id)
        if base is not None:
            request.user.favorites.filter(listing=base).delete()
        return Response(_payload(request.user))
