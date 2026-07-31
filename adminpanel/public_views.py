"""
Public (no-auth) endpoints backed by admin-managed data.

GET /api/banners — the customer home page's hero carousel. Returns only the
banners active today (IST); the frontend falls back to its static slides when
the list is empty. Cached ~60s like the other public reads.
"""
import datetime

from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.slots import today_ist

from .models import Settings

CACHE_SECONDS = 60


def _parse_date(text):
    try:
        return datetime.date.fromisoformat(str(text).strip())
    except ValueError:
        return None  # unparseable -> treated as open-ended


def _is_active(banner, today):
    """Active = has a title AND today falls inside the from/to window
    (either side empty = open-ended)."""
    if not isinstance(banner, dict):
        return False
    if not str(banner.get('title') or '').strip():
        return False

    start = _parse_date(banner.get('from') or '')
    if start is not None and start > today:
        return False
    end = _parse_date(banner.get('to') or '')
    if end is not None and end < today:
        return False
    return True


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicBannersView(APIView):
    """GET /api/banners -> {"banners": [ ...active banners, admin order... ]}."""

    permission_classes = [AllowAny]

    def get(self, request):
        today = today_ist()
        banners = Settings.load().banners or []
        return Response({
            'banners': [b for b in banners if _is_active(b, today)],
        })
