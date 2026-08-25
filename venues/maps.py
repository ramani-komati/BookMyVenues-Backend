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
from urllib.parse import urljoin, urlparse

import requests
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# Only these hosts may be resolved. Exact match — no subdomains.
ALLOWED_HOSTS = {'maps.app.goo.gl', 'goo.gl', 'g.co'}
TIMEOUT = 10  # seconds to wait for the redirect
MAX_REDIRECTS = 5   # bounded so one request cannot hold a worker indefinitely
MAX_URL_LENGTH = 512  # the cache key is derived from this — keep it bounded
# A resolved short link never changes — cache it for a long time.
CACHE_SECONDS = 60 * 60 * 24 * 30  # 30 days


class MapsError(Exception):
    """Bad/unsupported url, or the redirect could not be followed."""


def _is_allowed(url):
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.hostname in ALLOWED_HOSTS


# Where a Google short link is allowed to END UP.
RESOLVED_HOSTS = {
    'maps.google.com', 'www.google.com', 'google.com',
    'maps.app.goo.gl', 'goo.gl', 'g.co',
}


def _is_google_maps(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    # Google localises maps to country domains (google.co.in, google.de, ...).
    return host in RESOLVED_HOSTS or host.startswith('www.google.')


def resolve_short_link(url):
    """
    Follow a Google Maps short link and return the final URL.
    Raises MapsError for a bad/unsupported url or a failed redirect.
    """
    url = str(url or '').strip()
    if len(url) > MAX_URL_LENGTH:
        raise MapsError('That link is too long to be a Google Maps short link.')
    if not _is_allowed(url):
        raise MapsError(
            'Only Google Maps short links '
            '(maps.app.goo.gl, goo.gl, g.co) are supported.'
        )

    cached = cache.get(url)
    if cached:
        return cached

    # Follow the chain ONE HOP AT A TIME, re-checking the allowlist before
    # each request. requests' own allow_redirects would only validate the
    # first URL and then follow anything the chain pointed at — including a
    # private/internal address — and we hand the final URL back to an
    # unauthenticated caller.
    current = url
    resolved = None
    try:
        for _ in range(MAX_REDIRECTS):
            response = requests.get(
                current, allow_redirects=False, timeout=TIMEOUT
            )
            location = (getattr(response, 'headers', None) or {}).get('Location')
            if not (getattr(response, 'is_redirect', False) and location):
                break
            current = urljoin(current, location)
            # A Google short link must land on a Google MAPS url. Anything
            # else — including an internal/private address — stops here.
            if not _is_allowed(current) and not _is_google_maps(current):
                raise MapsError('That link redirects somewhere unexpected.')
            resolved = current
            if not _is_allowed(current):
                # No longer a short link: this IS the destination. Stop without
                # fetching it — we only ever return the URL, never the page.
                break
    except requests.RequestException as exc:
        logger.warning('Maps resolve failed: %s', exc)
        raise MapsError('Could not resolve the map link right now.') from exc

    if not resolved or resolved == url:
        raise MapsError('The link did not resolve to a full map URL.')

    cache.set(url, resolved, CACHE_SECONDS)
    return resolved


class MapsResolveView(APIView):
    """GET /api/maps/resolve?url=<shortlink> -> {"resolved": "<full url>"}."""

    permission_classes = [AllowAny]
    # Each miss makes a blocking outbound request, so an unthrottled caller
    # could occupy every worker with distinct un-cached URLs.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'maps'

    def get(self, request):
        try:
            resolved = resolve_short_link(request.query_params.get('url'))
        except MapsError as error:
            return Response({'message': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({'resolved': resolved})
