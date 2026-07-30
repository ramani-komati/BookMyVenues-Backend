"""
Google Maps short-link resolver (P2).

Vendors paste share links like `https://maps.app.goo.gl/XYZ` that carry no
coordinates — the exact pin only exists behind an HTTP redirect the browser
cannot follow (CORS). We follow it server-side and return the final URL.

Public, no auth. SECURITY: only the three Google short-link hosts may be
resolved — we never follow an arbitrary URL (SSRF guard). Only the final URL
string is returned, never the response body.
"""
import logging
from urllib.parse import urlparse

import requests
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# Only these hosts may be resolved. Exact match — no subdomains.
ALLOWED_HOSTS = {'maps.app.goo.gl', 'goo.gl', 'g.co'}
TIMEOUT = 10  # seconds to wait for the redirect
# A resolved short link never changes — cache it for a long time.
CACHE_SECONDS = 60 * 60 * 24 * 30  # 30 days


class MapsError(Exception):
    """Bad/unsupported url, or the redirect could not be followed."""


def _is_allowed(url):
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.hostname in ALLOWED_HOSTS


def resolve_short_link(url):
    """
    Follow a Google Maps short link and return the final URL.
    Raises MapsError for a bad/unsupported url or a failed redirect.
    """
    url = str(url or '').strip()
    if not _is_allowed(url):
        raise MapsError(
            'Only Google Maps short links '
            '(maps.app.goo.gl, goo.gl, g.co) are supported.'
        )

    cached = cache.get(url)
    if cached:
        return cached

    try:
        response = requests.get(url, allow_redirects=True, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.warning('Maps resolve failed for %s: %s', url, exc)
        raise MapsError('Could not resolve the map link right now.') from exc

    resolved = response.url
    if not resolved or resolved == url:
        raise MapsError('The link did not resolve to a full map URL.')

    cache.set(url, resolved, CACHE_SECONDS)
    return resolved


class MapsResolveView(APIView):
    """GET /api/maps/resolve?url=<shortlink> -> {"resolved": "<full url>"}."""

    permission_classes = [AllowAny]

    def get(self, request):
        try:
            resolved = resolve_short_link(request.query_params.get('url'))
        except MapsError as error:
            return Response({'message': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'resolved': resolved})
