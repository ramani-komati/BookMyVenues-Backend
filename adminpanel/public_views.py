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


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class PublicConfigView(APIView):
    """GET /api/config -> {"fee": 20, "feeDate": "2026-08-01"}.

    `fee` is the fee that applies TODAY (a future feeDate returns the default
    until it kicks in) — the exact value the booking recompute charges, so the
    customer bill and the server can never disagree."""

    permission_classes = [AllowAny]

    def get(self, request):
        from django.conf import settings as django_settings

        from bookings.razorpay_client import configured as payments_configured
        from venues.public_views import NEW_BADGE_DAYS

        settings_obj = Settings.load()
        # razorpayKeyId is PUBLIC by design (it goes into the checkout widget);
        # the secrets never leave the server. `paymentsMode` lets the frontend
        # tell test keys from live ones without guessing.
        key_id = django_settings.RAZORPAY_KEY_ID
        return Response({
            'fee': settings_obj.effective_fee(),
            'feeDate': settings_obj.fee_date,
            'newBadgeDays': NEW_BADGE_DAYS,
            'paymentsEnabled': payments_configured(),
            'razorpayKeyId': key_id,
            'paymentsMode': (
                'test' if key_id.startswith('rzp_test') else
                'live' if key_id.startswith('rzp_live') else ''
            ),
        })
