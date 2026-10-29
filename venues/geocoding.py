"""
Place search for the customer's manual location picker.

    GET /api/geo/search?q=gachibowli        -> {"results": [{label, locality, city, lat, lng}]}
    GET /api/geo/reverse?lat=17.44&lng=78.35 -> {"result": {label, locality, city} | null}

WHY A SERVER-SIDE PROXY

The project had no geocoding service, and coordinates for a typed place must
come from one — never from a guess. Proxying keeps any provider key on the
server (it never ships in the frontend bundle) and lets results be cached.

PROVIDERS (settings.GEOCODER_PROVIDER, set from the environment)

  ''          OFF (the default). Both endpoints answer 503 with
              code GEOCODER_NOT_CONFIGURED; the picker then offers "use my
              current location" only. Nothing is invented.
  'nominatim' OpenStreetMap Nominatim. No key, but its usage policy requires
              an identifying contact (GEOCODER_CONTACT_EMAIL — refused
              without it), at most ~1 request/second, and NO search-as-you-
              type: the picker searches only on submit. The public instance
              suits low traffic; point NOMINATIM_BASE_URL at a self-hosted or
              commercial Nominatim for production volume.
  'google'    Google Geocoding API. Needs GOOGLE_GEOCODING_API_KEY (billed
              per request by Google).

Results are restricted to GEOCODER_COUNTRY (default 'in'). The customer's
query and coordinates are sent to the configured provider only — never logged
or stored here; responses are cached briefly in the process cache.
"""
import hashlib
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .geo import valid_coordinates

logger = logging.getLogger(__name__)

TIMEOUT = 6            # seconds — a slow provider must not hold a worker
MAX_RESULTS = 5
MIN_QUERY, MAX_QUERY = 2, 100
CACHE_SECONDS = 60 * 60 * 24   # place names and pins change rarely


class GeocoderError(Exception):
    """The provider failed or answered something unusable."""


class GeocoderNotConfigured(GeocoderError):
    """No provider (or an incomplete one) is configured."""


def _provider():
    provider = str(getattr(settings, 'GEOCODER_PROVIDER', '') or '').strip().lower()
    if provider == 'nominatim':
        if not getattr(settings, 'GEOCODER_CONTACT_EMAIL', ''):
            raise GeocoderNotConfigured('Nominatim needs GEOCODER_CONTACT_EMAIL.')
        return provider
    if provider == 'google':
        if not getattr(settings, 'GOOGLE_GEOCODING_API_KEY', ''):
            raise GeocoderNotConfigured('Google geocoding needs GOOGLE_GEOCODING_API_KEY.')
        return provider
    raise GeocoderNotConfigured('No geocoding provider is configured.')


def _country():
    return str(getattr(settings, 'GEOCODER_COUNTRY', 'in') or 'in').strip().lower()


def _join(*parts):
    seen, out = set(), []
    for part in parts:
        text = str(part or '').strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return ', '.join(out)


def _place(locality, city, lat=None, lng=None, fallback=''):
    """One result row. A row without a usable name is dropped by callers."""
    locality = str(locality or '').strip()
    city = str(city or '').strip()
    row = {
        'label': _join(locality, city) or str(fallback or '').strip(),
        'locality': locality or city,
        'city': city,
    }
    if lat is not None:
        point = valid_coordinates(lat, lng)
        if point is None:
            return None
        row['lat'], row['lng'] = point
    return row if row['label'] else None


# --- Nominatim ------------------------------------------------------------

def _nominatim_get(path, params):
    base = str(getattr(settings, 'NOMINATIM_BASE_URL', '') or
               'https://nominatim.openstreetmap.org').rstrip('/')
    contact = settings.GEOCODER_CONTACT_EMAIL
    response = requests.get(
        f'{base}/{path}',
        params={**params, 'format': 'jsonv2', 'addressdetails': 1, 'email': contact},
        headers={'User-Agent': f'TheBookMyVenues/1.0 ({contact})'},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _nominatim_names(address):
    address = address if isinstance(address, dict) else {}
    locality = (
        address.get('suburb') or address.get('neighbourhood') or address.get('quarter')
        or address.get('city_district') or address.get('village') or address.get('town')
    )
    city = (
        address.get('city') or address.get('town') or address.get('state_district')
        or address.get('county')
    )
    return locality, city


def _nominatim_search(query):
    data = _nominatim_get('search', {
        'q': query, 'limit': MAX_RESULTS, 'countrycodes': _country(),
    })
    if not isinstance(data, list):
        raise GeocoderError('Unexpected search response.')
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        locality, city = _nominatim_names(item.get('address'))
        row = _place(locality or item.get('name'), city, item.get('lat'), item.get('lon'),
                     fallback=item.get('display_name'))
        if row:
            rows.append(row)
    return rows


def _nominatim_reverse(lat, lng):
    data = _nominatim_get('reverse', {'lat': lat, 'lon': lng, 'zoom': 14})
    if not isinstance(data, dict):
        raise GeocoderError('Unexpected reverse response.')
    if data.get('error'):
        return None   # e.g. the middle of the sea — "no name", not a failure
    return _place(*_nominatim_names(data.get('address')))


# --- Google ---------------------------------------------------------------

GOOGLE_URL = 'https://maps.googleapis.com/maps/api/geocode/json'


def _google_get(params):
    response = requests.get(
        GOOGLE_URL,
        params={**params, 'key': settings.GOOGLE_GEOCODING_API_KEY},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    status_text = data.get('status') if isinstance(data, dict) else None
    if status_text == 'ZERO_RESULTS':
        return []
    if status_text != 'OK':
        # REQUEST_DENIED / OVER_QUERY_LIMIT … — never echo Google's message
        # (it can describe the key's configuration) to the public.
        raise GeocoderError(f'Google geocoding status {status_text!r}.')
    return data.get('results') or []


def _google_names(components):
    found = {}
    for component in components or []:
        for kind in component.get('types') or []:
            found.setdefault(kind, component.get('long_name'))
    locality = (
        found.get('sublocality_level_1') or found.get('sublocality')
        or found.get('neighborhood')
    )
    city = (
        found.get('locality') or found.get('administrative_area_level_3')
        or found.get('administrative_area_level_2')
    )
    return locality, city


def _google_search(query):
    rows = []
    for item in _google_get({'address': query, 'components': f'country:{_country().upper()}'})[:MAX_RESULTS]:
        location = ((item.get('geometry') or {}).get('location') or {})
        locality, city = _google_names(item.get('address_components'))
        row = _place(locality, city, location.get('lat'), location.get('lng'),
                     fallback=item.get('formatted_address'))
        if row:
            rows.append(row)
    return rows


def _google_reverse(lat, lng):
    results = _google_get({'latlng': f'{lat},{lng}'})
    if not results:
        return None
    return _place(*_google_names(results[0].get('address_components')))


# --- Public API -----------------------------------------------------------

def _provider_failure(exc):
    """A GeocoderError that is safe to log.

    str() of a requests exception embeds the full request URL — for Google
    that includes `key=<GOOGLE_GEOCODING_API_KEY>`, and for every provider the
    customer's query or coordinates. Only the exception type and HTTP status
    are kept, so neither the key nor the customer's input reaches the logs.
    """
    response = getattr(exc, 'response', None)
    code = getattr(response, 'status_code', None)
    return GeocoderError(f'{type(exc).__name__}' + (f' (HTTP {code})' if code else ''))


def search_places(query):
    provider = _provider()
    key = 'geo:search:' + hashlib.sha256(f'{provider}|{query.lower()}'.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        rows = _nominatim_search(query) if provider == 'nominatim' else _google_search(query)
    except (requests.RequestException, ValueError) as exc:
        raise _provider_failure(exc) from None
    cache.set(key, rows, CACHE_SECONDS)
    return rows


def reverse_place(lat, lng):
    provider = _provider()
    # ~110 m buckets: enough for a locality name, and the cache never holds a
    # customer's exact position.
    lat, lng = round(lat, 3), round(lng, 3)
    key = f'geo:reverse:{provider}:{lat}:{lng}'
    cached = cache.get(key)
    if cached is not None:
        return cached or None
    try:
        row = _nominatim_reverse(lat, lng) if provider == 'nominatim' else _google_reverse(lat, lng)
    except (requests.RequestException, ValueError) as exc:
        raise _provider_failure(exc) from None
    cache.set(key, row or {}, CACHE_SECONDS)
    return row


def _unavailable(error):
    if isinstance(error, GeocoderNotConfigured):
        return Response(
            {
                'message': 'Place search is not available yet — use your current location instead.',
                'code': 'GEOCODER_NOT_CONFIGURED',
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    logger.warning('Geocoding provider failed: %s', error)
    return Response(
        {'message': 'Place search is temporarily unavailable. Please try again.',
         'code': 'GEOCODER_UNAVAILABLE'},
        status=status.HTTP_502_BAD_GATEWAY,
    )


class PlaceSearchView(APIView):
    """GET /api/geo/search?q= — places matching a typed name (on submit)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'geocode'

    def get(self, request):
        query = ' '.join(str(request.query_params.get('q', '')).split())
        if not (MIN_QUERY <= len(query) <= MAX_QUERY):
            return Response(
                {'message': f'Type {MIN_QUERY}–{MAX_QUERY} characters to search.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return Response({'results': search_places(query)})
        except GeocoderError as error:
            return _unavailable(error)


class PlaceReverseView(APIView):
    """GET /api/geo/reverse?lat=&lng= — the locality name for a point."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'geocode'

    def get(self, request):
        point = valid_coordinates(
            request.query_params.get('lat'), request.query_params.get('lng')
        )
        if point is None:
            return Response(
                {'message': 'lat and lng must be valid coordinates.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return Response({'result': reverse_place(*point)})
        except GeocoderError as error:
            return _unavailable(error)
