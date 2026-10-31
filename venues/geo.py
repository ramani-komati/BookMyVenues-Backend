"""
Geography — where a venue or event is, and how far it is from a customer.

WHERE COORDINATES COME FROM

Only from the Google Maps link the vendor (or, for events, the admin) saved,
under the SAME exactness rule as the frontend's utils/maps.js exactPinCoords:

  * the LAST `!3d<lat>!4d<lng>` pair in a place URL — the place marker itself
    (earlier pairs describe other map elements), or
  * a destination-style parameter whose value IS a coordinate pair
    (q / query / destination / daddr / ll, optionally prefixed "loc:"), or
  * a /maps/place|search|dir/ path segment that IS a coordinate pair — in
    decimal ("17.9689,79.5941") or degrees-minutes-seconds
    (17°58'08.0"N 79°35'38.8"E — what a dropped pin's link carries).

A short share link (maps.app.goo.gl) or a plain search link carries no pin, so
it yields None. Nothing is ever guessed from a venue's name or address, and
the `@lat,lng` viewport centre is deliberately ignored: it is where the map
happened to be scrolled, not where the venue is.

When the link has no pin, the vendor's own confirmed map location from the
registration wizard (`detail.mapPin`) is used instead — see record_map_pin.

CUSTOMER LOCATION

The customer's coordinates arrive as query parameters on a public list
request, are used to compute distances for that one response, and are never
stored. The frontend rounds them to 3 decimals (~110 m) before sending.
"""
import math
import re
from urllib.parse import parse_qs, unquote_plus, urlparse

# Mean Earth radius in km. The frontend's utils/geo.js uses the same constant,
# so a distance computed in the browser matches the one the API returns.
EARTH_RADIUS_KM = 6371.0

# Upper bound for ?radius= — wide enough for "anywhere in the state", small
# enough that the bounding-box prefilter stays meaningful. (The customer site
# offers 5–50 km; its default is 30 km — frontend constants/location.js.)
MAX_RADIUS_KM = 500

_MARKER = re.compile(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)')
_PAIR = re.compile(r'^\s*(?:loc:\s*)?(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$', re.I)
_PIN_PARAMS = ('q', 'query', 'destination', 'daddr', 'll')
# One hemisphere-suffixed degrees-minutes-seconds coordinate: 17°58'08.0"N
_DMS = re.compile(
    r'(\d{1,3})\s*°\s*(\d{1,2})\s*[\'′]\s*(\d{1,2}(?:\.\d+)?)\s*(?:"|″|\'\')?\s*([NSEW])', re.I,
)
# Path segments Google uses for a point: /maps/place/<pt>, /maps/search/<pt>,
# /maps/dir/<from>/<pt> (the LAST stop is the destination).
_PATH_KINDS = ('place', 'search', 'dir')


class LocationParamError(ValueError):
    """A malformed ?lat= / ?lng= / ?radius= on a list request."""


def valid_coordinates(lat, lng):
    """(lat, lng) as floats when both are finite and in range, else None.

    (0, 0) is refused: it is the classic unset default ("Null Island"), never
    a real venue in this catalogue.
    """
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    if lat == 0 and lng == 0:
        return None
    return lat, lng


def _dms_pair(text):
    """(lat, lng) from a degrees-minutes-seconds pair, or None."""
    found = _DMS.findall(str(text or ''))
    if len(found) != 2:
        return None
    values = {}
    for deg, minutes, seconds, hemi in found:
        if int(minutes) >= 60 or float(seconds) >= 60:
            return None
        value = int(deg) + int(minutes) / 60 + float(seconds) / 3600
        hemi = hemi.upper()
        values['lat' if hemi in 'NS' else 'lng'] = -value if hemi in 'SW' else value
    if set(values) != {'lat', 'lng'}:
        return None
    return valid_coordinates(round(values['lat'], 7), round(values['lng'], 7))


def _point_text(text):
    """A coordinate pair written as text (decimal or DMS), or None."""
    match = _PAIR.match(text)
    if match:
        return valid_coordinates(match.group(1), match.group(2))
    return _dms_pair(text)


def _path_point(path):
    """A coordinate pair that IS a /maps/place|search|dir/ path segment."""
    segments = [unquote_plus(part) for part in str(path or '').split('/')]
    for index, segment in enumerate(segments):
        if segment not in _PATH_KINDS:
            continue
        tail = [p for p in segments[index + 1:] if p and not p.startswith(('@', 'data='))]
        if not tail:
            return None
        return _point_text(tail[-1] if segment == 'dir' else tail[0])
    return None


def pin_from_maps_link(link):
    """The exact pin carried by a maps link as (lat, lng), or None."""
    text = str(link or '').strip()
    if not text:
        return None
    markers = _MARKER.findall(text)
    if markers:
        return valid_coordinates(*markers[-1])
    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
    except ValueError:
        return None
    for key in _PIN_PARAMS:
        values = query.get(key)
        # First value only — what URLSearchParams.get() reads in the browser.
        match = _PAIR.match(values[0]) if values else None
        if match:
            return valid_coordinates(match.group(1), match.group(2))
    return _path_point(parsed.path)


def place_text_from_maps_link(link):
    """The place NAME a maps link points at ("Stayfit Sports Zone, plot 6, …
    Hanamkonda") — from /maps/place/<name>/ or a text q/query parameter — or
    ''. A short link's resolved URL often carries only this (plus Google's
    internal place id), never coordinates. It is search text for an admin to
    confirm — never a location by itself."""
    text = str(link or '').strip()
    if not text:
        return ''
    try:
        parsed = urlparse(text)
        query = parse_qs(parsed.query)
    except ValueError:
        return ''
    segments = parsed.path.split('/')
    if 'place' in segments:
        index = segments.index('place')
        name = unquote_plus(segments[index + 1]).strip() if index + 1 < len(segments) else ''
        if name and not name.startswith('@') and _point_text(name) is None:
            return name[:300]
    for key in ('q', 'query', 'destination', 'daddr'):
        value = (query.get(key) or [''])[0].strip()
        if value and _point_text(value) is None:
            return value[:300]
    return ''


def record_maps_link(record):
    """The vendor's maps link from a listing record (detail.mapsLink, or a
    top-level mapsLink on older records)."""
    record = record if isinstance(record, dict) else {}
    detail = record.get('detail') if isinstance(record.get('detail'), dict) else {}
    return str(detail.get('mapsLink') or record.get('mapsLink') or '').strip()


def record_pin(record):
    """The exact pin in a listing record's maps link, or None.

    A short share link has no pin of its own; `detail.mapsLinkResolved`
    ({source, url}, written by `manage.py listing_coordinates
    --resolve-short-links`) supplies the full URL it redirects to — used ONLY
    while `source` is still the vendor's current link, so replacing the link
    can never leave a stale pin behind. The vendor's own link is never
    overwritten.
    """
    link = record_maps_link(record)
    point = pin_from_maps_link(link)
    if point is not None:
        return point
    detail = record.get('detail') if isinstance(record, dict) else None
    detail = detail if isinstance(detail, dict) else {}
    resolved = detail.get('mapsLinkResolved')
    if link and isinstance(resolved, dict) and str(resolved.get('source') or '').strip() == link:
        point = pin_from_maps_link(resolved.get('url'))
        if point is not None:
            return point
    return record_map_pin(record)


def record_map_pin(record):
    """The map location the vendor CONFIRMED in the wizard's Map location
    field (`detail.mapPin` = {lat, lng}) — set from their phone's GPS at the
    venue, their address, or typed coordinates. Used only when the maps link
    itself carries no exact pin: an exact link pin always wins."""
    detail = record.get('detail') if isinstance(record, dict) else None
    pin = detail.get('mapPin') if isinstance(detail, dict) else None
    if not isinstance(pin, dict):
        return None
    return valid_coordinates(pin.get('lat'), pin.get('lng'))


def draft_location_pin(location):
    """The pin a wizard Location section will publish: the exact pin in its
    maps link, else the vendor's confirmed latitude/longitude — or None."""
    location = location if isinstance(location, dict) else {}
    point = pin_from_maps_link(location.get('mapsLink'))
    if point is not None:
        return point
    lat, lng = location.get('latitude'), location.get('longitude')
    if lat in (None, '') or lng in (None, ''):
        return None
    return valid_coordinates(lat, lng)


def coordinates_payload(lat, lng):
    """{"lat", "lng"} for an API response, or None when either is missing."""
    point = valid_coordinates(lat, lng) if lat is not None and lng is not None else None
    return {'lat': point[0], 'lng': point[1]} if point else None


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance between two points, in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def distance_km(origin, lat, lng):
    """Distance from `origin` (lat, lng) to a stored point, rounded to 10 m,
    or None when the point has no valid coordinates."""
    point = valid_coordinates(lat, lng) if lat is not None and lng is not None else None
    if origin is None or point is None:
        return None
    return round(haversine_km(origin[0], origin[1], point[0], point[1]), 2)


def bounding_box(lat, lng, radius_km):
    """(min_lat, max_lat, min_lng, max_lng) enclosing the circle — a cheap
    indexed SQL prefilter. The exact Haversine check always runs afterwards,
    so the box only has to be big enough, never exact. Longitude bounds are
    None near the poles or across the antimeridian (no longitude filter)."""
    d_lat = math.degrees(radius_km / EARTH_RADIUS_KM)
    cos_lat = math.cos(math.radians(lat))
    min_lat, max_lat = max(-90.0, lat - d_lat), min(90.0, lat + d_lat)
    if cos_lat < 1e-6:
        return min_lat, max_lat, None, None
    d_lng = math.degrees(radius_km / (EARTH_RADIUS_KM * cos_lat))
    if lng - d_lng < -180 or lng + d_lng > 180:
        return min_lat, max_lat, None, None
    return min_lat, max_lat, lng - d_lng, lng + d_lng


def parse_location_params(params):
    """(origin, radius_km) from a list request's ?lat=&lng=&radius=.

    origin is (lat, lng) or None; radius_km is a float or None. Raises
    LocationParamError for half a coordinate pair, junk values, or a radius
    without a location (a radius around nowhere means nothing).
    """
    raw_lat = str(params.get('lat', '')).strip()
    raw_lng = str(params.get('lng', '')).strip()
    raw_radius = str(params.get('radius', '')).strip()

    origin = None
    if raw_lat or raw_lng:
        if not (raw_lat and raw_lng):
            raise LocationParamError('lat and lng must be sent together.')
        origin = valid_coordinates(raw_lat, raw_lng)
        if origin is None:
            raise LocationParamError('lat/lng must be valid coordinates.')

    radius = None
    if raw_radius:
        if origin is None:
            raise LocationParamError('radius needs lat and lng.')
        try:
            radius = float(raw_radius)
        except ValueError:
            raise LocationParamError('radius must be a number of kilometres.') from None
        if not (math.isfinite(radius) and 0 < radius <= MAX_RADIUS_KM):
            raise LocationParamError(
                f'radius must be between 0 and {MAX_RADIUS_KM} km.'
            )
    return origin, radius


def has_coordinates(lat, lng):
    """True when a stored point is a usable pin (the rule distance_km uses)."""
    return lat is not None and lng is not None and valid_coordinates(lat, lng) is not None


def rank_by_distance(rows, origin, radius_km=None, *, nearest_first=False):
    """Distances for `rows` — an ordered list of (key, lat, lng) — as
    (ordered keys, {key: distance or None}).

    radius_km EXCLUDES rows outside it, including rows without coordinates
    (they cannot be shown to be inside). nearest_first ORDERS by distance,
    keeping rows without coordinates at the end in their original order; the
    sort is stable, so equal distances keep the caller's order too.
    """
    distances = {key: distance_km(origin, lat, lng) for key, lat, lng in rows}
    keys = [key for key, _lat, _lng in rows]
    if radius_km is not None:
        keys = [
            key for key in keys
            if distances[key] is not None and distances[key] <= radius_km
        ]
    if nearest_first:
        keys.sort(key=lambda key: (distances[key] is None, distances[key] or 0.0))
    return keys, distances
