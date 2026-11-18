"""
Google Maps short-link resolver (P2).

Vendors paste share links like `https://maps.app.goo.gl/XYZ` that carry no
coordinates — the exact pin only exists behind an HTTP redirect the browser
cannot follow (CORS). We follow it server-side and return the final URL.

Public, no auth. SECURITY: only Google short-link hosts may be resolved —
we never follow an arbitrary URL (SSRF guard). Only the final URL string is
returned, never the response body.

Supported inputs (QA audit 2026-10-30 — links were not reliably converted):
  * short links: maps.app.goo.gl/…, goo.gl/maps/…, g.co/…, g.page/…
  * full Google Maps URLs (google.com/maps…, maps.google.com, google.co.in/maps…)
    — answered WITHOUT any network call.
Every answer carries the pin read from the final URL (`coordinates`, null
when the link has none — Google's place links often carry only a name) and
the place name (`placeName`), so callers never re-parse it. A link that could
not be resolved is cached as a failure for a while, so one bad link cannot
make the server hammer Google.
"""
import hashlib
import logging
import time
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

# Only these hosts may be resolved. Exact match — no subdomains.
ALLOWED_HOSTS = {'maps.app.goo.gl', 'goo.gl', 'g.co', 'g.page'}
TIMEOUT = 10  # seconds to wait for the redirect
MAX_REDIRECTS = 5   # bounded so one request cannot hold a worker indefinitely
MAX_URL_LENGTH = 512  # the cache key is derived from this — keep it bounded
# A resolved short link never changes — cache it for a long time.
CACHE_SECONDS = 60 * 60 * 24 * 30  # 30 days
FAILURE_CACHE_SECONDS = 60 * 10    # a failed link is not retried for 10 min
RETRY_DELAY = 1.0                  # one retry after a dropped connection


class MapsError(Exception):
    """Bad/unsupported url, or the redirect could not be followed."""


def _is_allowed(url):
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and parsed.hostname in ALLOWED_HOSTS


# Where a Google short link is allowed to END UP.
RESOLVED_HOSTS = {
    'maps.google.com', 'www.google.com', 'google.com',
    'maps.app.goo.gl', 'goo.gl', 'g.co', 'g.page',
}
CONSENT_HOST = 'consent.google.com'  # cookie interstitial: ?continue=<maps url>


def _is_google_maps(url):
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower()
    # Google localises maps to country domains (google.co.in, google.de, ...).
    return host in RESOLVED_HOSTS or host.startswith('www.google.')


def is_full_maps_url(url):
    """A full Google Maps URL (no redirect to follow)."""
    parsed = urlparse(str(url or ''))
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or host in ALLOWED_HOSTS:
        return False
    if host == 'maps.google.com' or host.startswith('maps.google.'):
        return True
    return (host == 'google.com' or host.startswith('www.google.')) and parsed.path.startswith('/maps')


def _cache_key(url):
    return 'maps:resolve:' + hashlib.sha256(url.encode()).hexdigest()


def _unwrap_consent(url):
    """consent.google.com?continue=<maps url> → the maps url, else url."""
    parsed = urlparse(url)
    if (parsed.hostname or '').lower() != CONSENT_HOST:
        return url
    target = (parse_qs(parsed.query).get('continue') or [''])[0]
    return target if _is_google_maps(target) else url


def _fetch(url):
    """One redirect hop, retried once after a dropped connection/timeout."""
    try:
        return requests.get(url, allow_redirects=False, timeout=TIMEOUT)
    except (requests.ConnectionError, requests.Timeout):
        time.sleep(RETRY_DELAY)
        return requests.get(url, allow_redirects=False, timeout=TIMEOUT)


def resolve_short_link(url):
    """
    Follow a Google Maps short link and return the final URL.
    Raises MapsError for a bad/unsupported url or a failed redirect.
    """
    url = str(url or '').strip()
    if len(url) > 2048:
        raise MapsError('That link is too long to be a Google Maps link.')
    if is_full_maps_url(url):
        return url  # already the destination — nothing to follow
    if len(url) > MAX_URL_LENGTH:
        raise MapsError('That link is too long to be a Google Maps short link.')
    if not _is_allowed(url):
        raise MapsError(
            'Only Google Maps links are supported (a maps.app.goo.gl share '
            'link or a google.com/maps address).'
        )

    key = _cache_key(url)
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get('error'):
        raise MapsError(cached['error'])
    if cached:
        return cached
    try:
        resolved = _follow(url)
    except MapsError as error:
        cache.set(key, {'error': str(error)}, FAILURE_CACHE_SECONDS)
        raise
    cache.set(key, resolved, CACHE_SECONDS)
    return resolved


def _follow(url):
    """Follow `url` hop by hop; the final Google Maps URL or MapsError."""
    # Follow the chain ONE HOP AT A TIME, re-checking the allowlist before
    # each request. requests' own allow_redirects would only validate the
    # first URL and then follow anything the chain pointed at — including a
    # private/internal address — and we hand the final URL back to an
    # unauthenticated caller.
    current = url
    resolved = None
    try:
        for _ in range(MAX_REDIRECTS):
            response = _fetch(current)
            location = (getattr(response, 'headers', None) or {}).get('Location')
            if not (getattr(response, 'is_redirect', False) and location):
                break
            current = _unwrap_consent(urljoin(current, location))
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
        # Type only: str(exc) carries the URL — the vendor's input.
        logger.warning('Maps resolve failed: %s', type(exc).__name__)
        raise MapsError('Could not resolve the map link right now.') from exc

    if not resolved or resolved == url:
        raise MapsError('The link did not resolve to a full map URL.')
    return resolved


def describe(resolved):
    """{resolved, coordinates, exact, placeName} for an API answer."""
    from .geo import pin_from_maps_link, place_text_from_maps_link
    point = pin_from_maps_link(resolved)
    return {
        'resolved': resolved,
        'coordinates': {'lat': point[0], 'lng': point[1]} if point else None,
        'exact': point is not None,
        'placeName': place_text_from_maps_link(resolved),
    }


class MapsResolveView(APIView):
    """GET /api/maps/resolve?url=<link> ->
    {"resolved": "<full url>", "coordinates": {lat, lng} | null,
     "exact": bool, "placeName": "…"};
    400 {"message", "code": "MAPS_LINK_INVALID"} for anything else."""

    permission_classes = [AllowAny]
    # Each miss makes a blocking outbound request, so an unthrottled caller
    # could occupy every worker with distinct un-cached URLs.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'maps'

    def get(self, request):
        try:
            resolved = resolve_short_link(request.query_params.get('url'))
        except MapsError as error:
            return Response(
                {'message': str(error), 'code': 'MAPS_LINK_INVALID'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(describe(resolved))
