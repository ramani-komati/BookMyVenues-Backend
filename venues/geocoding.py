"""
Place search for the customer's location picker and the vendor wizard.

    GET /api/geo/search?q=gachibowli[&state=&district=]
        -> {"results": [PLACE, …]}                  (geocode a typed name, on submit)
    GET /api/geo/reverse?lat=17.44&lng=78.35
        -> {"result": PLACE | null}                  (name the customer's GPS point)
    GET /api/geo/autocomplete?q=kompa[&session=&lat=&lng=]
        -> {"suggestions": [{placeId, main, secondary, label}]}   (as you type)
    GET /api/geo/place?id=<placeId>[&session=]
        -> {"result": PLACE | null}                  (the picked suggestion's details)
    GET /api/geo/regions
        -> {"country": "India", "states": [{name, code, type, districts}]}

    PLACE = {label, locality, city, district, state, country, formattedAddress, lat, lng}
            (lat/lng absent on reverse results — the customer's own point is used)

WHY A SERVER-SIDE PROXY

Coordinates for a typed place must come from a geocoder — never from a guess.
Proxying keeps the provider key on the server (it never ships in the frontend
bundle) and lets results be cached.

PROVIDERS (settings.GEOCODER_PROVIDER, set from the environment)

  ''          OFF (the default). Every endpoint except /regions answers 503
              with code GEOCODER_NOT_CONFIGURED; the picker then falls back
              to its own lists and to OpenStreetMap from the browser.
  'nominatim' OpenStreetMap Nominatim. No key, but its usage policy requires
              an identifying contact (GEOCODER_CONTACT_EMAIL — refused
              without it), at most ~1 request/second, and NO search-as-you-
              type: autocomplete is unavailable (503) with this provider.
  'google'    Google Geocoding API (search, reverse) with
              GOOGLE_GEOCODING_API_KEY, and Places API (New) (autocomplete,
              place) with GOOGLE_PLACES_API_KEY — or the geocoding key when
              that is unset. Both are SERVER keys: restrict them by API, not
              by HTTP referrer (Google refuses referrer-restricted keys on
              server calls with REQUEST_DENIED).

FAILURES (QA audit 2026-10-30: production logged REQUEST_DENIED — billing
off — on every request)

  * A refusal that will not fix itself (REQUEST_DENIED, OVER_DAILY_LIMIT,
    HTTP 401/403) opens a CIRCUIT for GEOCODER_BREAKER_SECONDS (15 min): no
    request reaches Google until it closes, so a misconfigured project costs
    one log line per window instead of one per customer.
  * A transient failure (timeout, dropped connection, HTTP 5xx,
    OVER_QUERY_LIMIT) is retried ONCE after a short pause.
  * While Google is unavailable, search and reverse fall back to
    OpenStreetMap Nominatim when GEOCODER_CONTACT_EMAIL is set (its usage
    policy needs a contact); requests to it are spaced ≥1.1 s apart.
  * A failed lookup is remembered for a minute, so retries from the same
    page do not multiply provider calls.

Results are restricted to GEOCODER_COUNTRY (default 'in'). State and district
names are matched to the platform's India dataset (venues/regions.py), so a
place always reports the same spelling the vendor wizard stores. The
customer's query and coordinates go to the configured provider only — never
logged or stored here; responses are cached briefly in the process cache.
"""
import hashlib
import logging
import re
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import regions
from .geo import valid_coordinates

logger = logging.getLogger(__name__)

TIMEOUT = 6            # seconds — a slow provider must not hold a worker
MAX_RESULTS = 5
MAX_SUGGESTIONS = 6
MIN_QUERY, MAX_QUERY = 2, 100
CACHE_SECONDS = 60 * 60 * 24   # place names and pins change rarely
BIAS_RADIUS_M = 50000          # autocomplete prefers places within ~50 km of a hint

BREAKER_KEY = 'geo:breaker:google'
BREAKER_SECONDS = 60 * 15        # a denied project is not retried for 15 min
FAILURE_SECONDS = 60             # a failed lookup is not repeated for 1 min
RETRY_PAUSE = 0.5                # before the one retry of a transient error
NOMINATIM_GAP = 1.1              # Nominatim allows ~1 request per second
# Google answers that will not fix themselves on retry.
_PERMANENT_STATUSES = {'REQUEST_DENIED', 'OVER_DAILY_LIMIT', 'INVALID_REQUEST'}
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]

_SESSION = re.compile(r'^[A-Za-z0-9_-]{8,64}$')
_PLACE_ID = re.compile(r'^[A-Za-z0-9_-]{10,300}$')


class GeocoderError(Exception):
    """The provider failed or answered something unusable."""


class GeocoderNotConfigured(GeocoderError):
    """No provider (or an incomplete one) is configured."""


class GeocoderDenied(GeocoderError):
    """The provider refused the project itself (billing, key, API off) —
    retrying cannot help until someone fixes the account."""


class GeocoderTransient(GeocoderError):
    """A failure worth exactly one retry (timeout, 5xx, rate limit)."""


def breaker_open():
    """True while Google is known to refuse this project."""
    return bool(cache.get(BREAKER_KEY))


def _open_breaker(reason):
    seconds = int(getattr(settings, 'GEOCODER_BREAKER_SECONDS', BREAKER_SECONDS) or BREAKER_SECONDS)
    if not cache.get(BREAKER_KEY):
        logger.error('Google geocoding disabled for %ss: %s', seconds, reason)
    cache.set(BREAKER_KEY, str(reason)[:300], seconds)


def _fallback_available():
    """OpenStreetMap can stand in for Google (needs an identifying contact)."""
    return bool(getattr(settings, 'GEOCODER_CONTACT_EMAIL', ''))


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


def _places_key():
    """Places API (New) key — only with the Google provider."""
    if _provider() != 'google':
        raise GeocoderNotConfigured('Autocomplete needs GEOCODER_PROVIDER=google.')
    return (getattr(settings, 'GOOGLE_PLACES_API_KEY', '')
            or settings.GOOGLE_GEOCODING_API_KEY)


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


def _region_names(state, district_candidates):
    """(canonical state, canonical district) from a provider's names — the
    first candidate that is a real district of that state wins; '' when
    nothing matches (never a guess)."""
    known_state = regions.canonical_state(state)
    for candidate in district_candidates:
        if known_state:
            found = regions.find_district(known_state, candidate)
            if found:
                return known_state, found
        else:
            pair = regions.find_district_anywhere(candidate)
            if pair:
                return pair
    return known_state or str(state or '').strip(), ''


def _place(locality, city, lat=None, lng=None, fallback='', *, district='', state='',
           formatted=''):
    """One result row. A row without a usable name is dropped by callers."""
    locality = str(locality or '').strip()
    city = str(city or '').strip()
    row = {
        'label': _join(locality, city) or str(fallback or '').strip(),
        'locality': locality or city,
        'city': city,
        'district': str(district or '').strip(),
        'state': str(state or '').strip(),
        'country': 'India' if _country() == 'in' else '',
        'formattedAddress': str(formatted or fallback or '').strip(),
    }
    if lat is not None:
        point = valid_coordinates(lat, lng)
        if point is None:
            return None
        row['lat'], row['lng'] = point
    return row if row['label'] else None


def _scoped_query(query, state='', district=''):
    """'Kompally' within Medchal Malkajgiri, Telangana → one geocodable text."""
    state_name = regions.canonical_state(state) or ''
    district_name = regions.find_district(state_name, district) if state_name else None
    lowered = query.lower()
    extra = [part for part in (district_name, state_name) if part and part.lower() not in lowered]
    return _join(query, *extra)


# --- Nominatim ------------------------------------------------------------

def _nominatim_get(path, params):
    base = str(getattr(settings, 'NOMINATIM_BASE_URL', '') or
               'https://nominatim.openstreetmap.org').rstrip('/')
    contact = settings.GEOCODER_CONTACT_EMAIL
    with _nominatim_lock:  # usage policy: at most ~1 request/second
        wait = _nominatim_last[0] + NOMINATIM_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _nominatim_last[0] = time.monotonic()
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


def _nominatim_regions(address):
    address = address if isinstance(address, dict) else {}
    return _region_names(address.get('state'), (
        address.get('state_district'), address.get('county'), address.get('city'),
    ))


def _nominatim_row(item, with_point=True):
    address = item.get('address')
    locality, city = _nominatim_names(address)
    state, district = _nominatim_regions(address)
    point = (item.get('lat'), item.get('lon')) if with_point else (None, None)
    return _place(locality or (item.get('name') if with_point else None), city, *point,
                  fallback=item.get('display_name') if with_point else '',
                  district=district, state=state, formatted=item.get('display_name'))


def _nominatim_search(query):
    data = _nominatim_get('search', {
        'q': query, 'limit': MAX_RESULTS, 'countrycodes': _country(),
    })
    if not isinstance(data, list):
        raise GeocoderError('Unexpected search response.')
    return [row for row in (_nominatim_row(item) for item in data if isinstance(item, dict)) if row]


def _nominatim_reverse(lat, lng):
    data = _nominatim_get('reverse', {'lat': lat, 'lon': lng, 'zoom': 14})
    if not isinstance(data, dict):
        raise GeocoderError('Unexpected reverse response.')
    if data.get('error'):
        return None   # e.g. the middle of the sea — "no name", not a failure
    return _nominatim_row(data, with_point=False)


# --- Google ---------------------------------------------------------------

GOOGLE_URL = 'https://maps.googleapis.com/maps/api/geocode/json'
PLACES_URL = 'https://places.googleapis.com/v1'
# Place Details "Essentials" fields only — nothing billed at a higher tier.
PLACE_FIELDS = 'id,formattedAddress,location,addressComponents'


def _http_status(response):
    code = getattr(response, 'status_code', 200)
    return code if isinstance(code, int) else 200


def _google_get(params):
    if breaker_open():
        raise GeocoderDenied('Google geocoding is paused after a refusal.')
    try:
        response = requests.get(
            GOOGLE_URL,
            params={**params, 'key': settings.GOOGLE_GEOCODING_API_KEY},
            timeout=TIMEOUT,
        )
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise GeocoderTransient(type(exc).__name__) from None
    code = _http_status(response)
    if code in (401, 403):
        _open_breaker(f'HTTP {code}')
        raise GeocoderDenied(f'HTTP {code}')
    if code >= 500 or code == 429:
        raise GeocoderTransient(f'HTTP {code}')
    response.raise_for_status()
    data = response.json()
    status_text = data.get('status') if isinstance(data, dict) else None
    if status_text == 'ZERO_RESULTS':
        return []
    if status_text != 'OK':
        # REQUEST_DENIED / OVER_QUERY_LIMIT … — Google's own explanation goes
        # to the SERVER LOG only (it names the cause: API not enabled, key
        # restricted by referrer, billing off), never to the public.
        detail = str((data or {}).get('error_message') or '')[:300] if isinstance(data, dict) else ''
        reason = f'Google geocoding status {status_text!r}' + (f': {detail}' if detail else '.')
        if status_text in _PERMANENT_STATUSES:
            _open_breaker(reason)
            raise GeocoderDenied(reason)
        if status_text in ('OVER_QUERY_LIMIT', 'UNKNOWN_ERROR'):
            raise GeocoderTransient(reason)
        raise GeocoderError(reason)
    return data.get('results') or []


def _components(raw, long_key, short_key):
    """{type: (long, short)} from Geocoding (long_name/short_name) or Places
    (New) (longText/shortText) address components — first one of a type wins."""
    found = {}
    for component in raw or []:
        if not isinstance(component, dict):
            continue
        for kind in component.get('types') or []:
            found.setdefault(kind, (component.get(long_key), component.get(short_key)))
    return found


def _google_names(components):
    found = {kind: names[0] for kind, names in components.items()}
    locality = (
        found.get('sublocality_level_1') or found.get('sublocality')
        or found.get('neighborhood')
    )
    city = (
        found.get('locality') or found.get('administrative_area_level_3')
        or found.get('administrative_area_level_2')
    )
    return locality, city


def _google_regions(components):
    state_long, state_short = components.get('administrative_area_level_1', (None, None))
    state = state_long if regions.find_state(state_long) else (state_short or state_long)
    found = {kind: names[0] for kind, names in components.items()}
    # Google files Indian districts under level 3 in some states and level 2
    # in others; the locality is tried last (Hyderabad is both).
    return _region_names(state, (
        found.get('administrative_area_level_3'), found.get('administrative_area_level_2'),
        found.get('locality'),
    ))


def _google_row(components, lat=None, lng=None, formatted=''):
    locality, city = _google_names(components)
    state, district = _google_regions(components)
    return _place(locality, city, lat, lng, fallback=formatted,
                  district=district, state=state, formatted=formatted)


def _google_search(query):
    rows = []
    for item in _google_get({'address': query, 'components': f'country:{_country().upper()}'})[:MAX_RESULTS]:
        location = ((item.get('geometry') or {}).get('location') or {})
        components = _components(item.get('address_components'), 'long_name', 'short_name')
        row = _google_row(components, location.get('lat'), location.get('lng'),
                          item.get('formatted_address'))
        if row:
            rows.append(row)
    return rows


def _google_reverse(lat, lng):
    results = _google_get({'latlng': f'{lat},{lng}'})
    if not results:
        return None
    first = results[0]
    components = _components(first.get('address_components'), 'long_name', 'short_name')
    # No lat/lng in the row: the customer's point stays theirs; only the
    # NAME comes from Google.
    return _google_row(components, formatted=first.get('formatted_address'))


def _places_call(method, path, **kwargs):
    """One Places API (New) request. Errors carry Google's status + message
    for the server log (never the key — it travels in a header)."""
    headers = {'X-Goog-Api-Key': _places_key(), **kwargs.pop('headers', {})}
    if breaker_open():
        raise GeocoderDenied('Google Places is paused after a refusal.')
    try:
        response = requests.request(method, f'{PLACES_URL}/{path}', headers=headers,
                                    timeout=TIMEOUT, **kwargs)
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise GeocoderTransient(type(exc).__name__) from None
    code = _http_status(response)
    if code in (401, 403):
        _open_breaker(f'Places API HTTP {code}')
    if code >= 500 or code == 429:
        raise GeocoderTransient(f'Places API HTTP {code}')
    if response.status_code != 200:
        try:
            error = (response.json() or {}).get('error') or {}
        except ValueError:
            error = {}
        raise GeocoderError(
            f'Places API HTTP {response.status_code} {error.get("status") or ""}: '
            f'{str(error.get("message") or "")[:300]}'
        )
    return response.json()


def _places_autocomplete(query, session='', bias=None):
    body = {
        'input': query,
        'includedRegionCodes': [_country()],
        # Cities, towns, districts, localities and PIN codes — not shops.
        'includedPrimaryTypes': ['(regions)'],
        'languageCode': 'en',
    }
    if session:
        body['sessionToken'] = session
    if bias:
        body['locationBias'] = {'circle': {
            'center': {'latitude': bias[0], 'longitude': bias[1]}, 'radius': BIAS_RADIUS_M,
        }}
    data = _places_call('POST', 'places:autocomplete', json=body)
    out = []
    for suggestion in (data or {}).get('suggestions') or []:
        prediction = (suggestion or {}).get('placePrediction') or {}
        place_id = str(prediction.get('placeId') or '')
        fmt = prediction.get('structuredFormat') or {}
        main = str((fmt.get('mainText') or {}).get('text') or '').strip()
        secondary = str((fmt.get('secondaryText') or {}).get('text') or '').strip()
        label = str((prediction.get('text') or {}).get('text') or '').strip() or _join(main, secondary)
        if _PLACE_ID.fullmatch(place_id) and (main or label):
            out.append({'placeId': place_id, 'main': main or label, 'secondary': secondary, 'label': label})
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


def _places_details(place_id, session=''):
    params = {'languageCode': 'en', 'regionCode': _country()}
    if session:
        params['sessionToken'] = session
    data = _places_call('GET', f'places/{place_id}', params=params,
                        headers={'X-Goog-FieldMask': PLACE_FIELDS})
    location = (data or {}).get('location') or {}
    components = _components((data or {}).get('addressComponents'), 'longText', 'shortText')
    return _google_row(components, location.get('latitude'), location.get('longitude'),
                       (data or {}).get('formattedAddress'))


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


def _attempt(compute):
    """compute(), retried once after a transient failure."""
    try:
        return compute()
    except GeocoderTransient:
        time.sleep(RETRY_PAUSE)
        return compute()


def _cached(key, compute, empty=None, fallback=None):
    """A cached provider answer. On failure: Nominatim `fallback` when one is
    given and allowed, else GeocoderError — remembered for FAILURE_SECONDS so
    the same lookup does not reach the provider again straight away."""
    cached = cache.get(key)
    if cached is not None:
        if isinstance(cached, dict) and cached.get('__failed__'):
            raise GeocoderError('Recent lookup failed; not retried yet.')
        return cached if cached != {} else empty
    try:
        value = _attempt(compute)
    except (GeocoderError, requests.RequestException, ValueError) as exc:
        error = exc if isinstance(exc, GeocoderError) else _provider_failure(exc)
        if fallback is not None and not isinstance(error, GeocoderNotConfigured) and _fallback_available():
            try:
                value = fallback()
            except (requests.RequestException, ValueError, GeocoderError) as fb_exc:
                logger.warning('Geocoding fallback failed too: %s', type(fb_exc).__name__)
            else:
                # Short cache: Google may be back once billing is fixed.
                cache.set(key, value if value is not None else {}, FAILURE_SECONDS * 10)
                return value
        cache.set(key, {'__failed__': True}, FAILURE_SECONDS)
        raise error from None
    cache.set(key, value if value is not None else {}, CACHE_SECONDS)
    return value


def _digest(*parts):
    return hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()


def scope_results(rows, state='', district=''):
    """Places in the requested state (and district) first, each marked
    `inScope`. When ANY row is in the requested state, rows from other states
    are dropped: "Kazipet" scoped to Telangana must not offer a same-named
    place elsewhere in India as if it were local."""
    rows = [dict(row) for row in rows or []]
    want_state = regions.canonical_state(state) or ''
    want_district = (regions.find_district(want_state, district) if want_state else None) or ''
    if not want_state:
        return rows  # unscoped search: provider order, shape unchanged
    for row in rows:
        same_state = regions.canonical_state(row.get('state')) == want_state
        row['inScope'] = same_state and (not want_district or row.get('district') == want_district)
        row['_state'] = same_state
    if any(row['_state'] for row in rows):
        rows = [row for row in rows if row['_state']]
    rows.sort(key=lambda row: not row['inScope'])  # stable: provider order kept
    for row in rows:
        row.pop('_state', None)
    return rows


def search_places(query, state='', district=''):
    provider = _provider()
    text = _scoped_query(query, state, district)
    key = 'geo:search:' + _digest(provider, text.lower())
    rows = _cached(
        key,
        lambda: _nominatim_search(text) if provider == 'nominatim' else _google_search(text),
        fallback=(lambda: _nominatim_search(text)) if provider == 'google' else None,
    )
    return scope_results(rows, state, district)


def reverse_place(lat, lng):
    provider = _provider()
    # ~110 m buckets: enough for a locality name, and the cache never holds a
    # customer's exact position.
    lat, lng = round(lat, 3), round(lng, 3)
    key = f'geo:reverse:{provider}:{lat}:{lng}'
    return _cached(key, lambda: _nominatim_reverse(lat, lng) if provider == 'nominatim'
                   else _google_reverse(lat, lng),
                   fallback=(lambda: _nominatim_reverse(lat, lng)) if provider == 'google' else None)


def autocomplete_places(query, session='', bias=None):
    _places_key()
    # The bias is rounded to ~1 km for the cache key and the request alike.
    bias = (round(bias[0], 2), round(bias[1], 2)) if bias else None
    key = 'geo:auto:' + _digest(query.lower(), bias)
    return _cached(key, lambda: _places_autocomplete(query, session, bias))


def place_details(place_id, session=''):
    _places_key()
    key = 'geo:place:' + _digest(place_id)
    return _cached(key, lambda: _places_details(place_id, session))


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


def _query_param(request):
    return ' '.join(str(request.query_params.get('q', '')).split())


def _bad_query():
    return Response(
        {'message': f'Type {MIN_QUERY}–{MAX_QUERY} characters to search.'},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _session(request):
    value = str(request.query_params.get('session', '') or '')
    return value if _SESSION.fullmatch(value) else ''


class PlaceSearchView(APIView):
    """GET /api/geo/search?q=[&state=&district=] — places matching a typed
    name (on submit). state/district narrow the search to that district."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'geocode'

    def get(self, request):
        query = _query_param(request)
        if not (MIN_QUERY <= len(query) <= MAX_QUERY):
            return _bad_query()
        state = str(request.query_params.get('state', '') or '')[:100]
        district = str(request.query_params.get('district', '') or '')[:100]
        try:
            return Response({'results': search_places(query, state, district)})
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


class PlaceAutocompleteView(APIView):
    """GET /api/geo/autocomplete?q=[&session=&lat=&lng=] — suggestions while
    the customer types (Places API (New); Google provider only). lat/lng
    only BIAS the suggestions towards a chosen district; nothing is filtered
    out by them."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'geocode_suggest'

    def get(self, request):
        query = _query_param(request)
        if not (MIN_QUERY <= len(query) <= MAX_QUERY):
            return _bad_query()
        params = request.query_params
        bias = None
        if params.get('lat') not in (None, '') or params.get('lng') not in (None, ''):
            bias = valid_coordinates(params.get('lat'), params.get('lng'))
            if bias is None:
                return Response({'message': 'lat and lng must be valid coordinates.'},
                                status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({'suggestions': autocomplete_places(query, _session(request), bias)})
        except GeocoderError as error:
            return _unavailable(error)


class PlaceDetailsView(APIView):
    """GET /api/geo/place?id=<placeId>[&session=] — the coordinates and
    address parts of a picked suggestion (ends the autocomplete session)."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'geocode'

    def get(self, request):
        place_id = str(request.query_params.get('id', '') or '')
        if not _PLACE_ID.fullmatch(place_id):
            return Response({'message': 'id must be a place id from /api/geo/autocomplete.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response({'result': place_details(place_id, _session(request))})
        except GeocoderError as error:
            return _unavailable(error)


@method_decorator(cache_page(CACHE_SECONDS), name='get')
class RegionListView(APIView):
    """GET /api/geo/regions — every Indian state / UT and its districts
    (venues/regions.py). Static data: needs no provider and no key."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({
            'country': regions.dataset()['country'],
            'states': [
                {'name': s['name'], 'code': s['code'], 'type': s['type'], 'districts': s['districts']}
                for s in regions.states()
            ],
        })
