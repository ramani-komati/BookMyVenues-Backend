"""
Event location normalisation — how a typed place is matched and displayed.

WHY THIS EXISTS

Organisers type places however they like. The same Dandiya ground arrives
as city "Warangal" with locality "Hanamkonda", as city "Hanamkonda", as
"Hanumakonda" (the official district spelling) or with everything packed
into the address line. A plain `city__icontains='Warangal'` misses three of
those four, which is how a published Hanamkonda event never showed up when
a customer searched Warangal.

Two rules fix that without geocoding or inventing data:

1. SPELLINGS of one place are the same place: Hanamkonda / Hanumakonda /
   Hanmakonda all match each other.
2. An AREA name also matches the localities inside it: searching "Warangal"
   (the tri-city the site serves) matches events in Hanamkonda and Kazipet.
   The reverse does NOT hold — searching "Kazipet" does not return an event
   on the other side of the city.

Matching runs over every place field (venue name, address, locality, city),
so a place is found wherever the organiser happened to type it. Nothing here
writes to an event: stored fields are always exactly what the admin entered.
"""
import re
from urllib.parse import urlparse

from django.db.models import Q

from bookings.slots import SlotError

# Each tuple is ONE place under its different spellings. Lower-case.
SPELLINGS = (
    ('hanamkonda', 'hanumakonda', 'hanmakonda', 'hanamakonda'),
    ('kazipet', 'kajipet', 'khazipet'),
    ('warangal', 'orugallu'),
)

# Area → the places inside it (by their first spelling above). Searching the
# area matches all of them; searching one of them matches only itself.
AREAS = {
    'warangal': ('warangal', 'hanamkonda', 'kazipet'),
}

# The text fields an event's place can be written in.
PLACE_FIELDS = ('venue_name', 'address', 'locality', 'city')
# Plus what a free-text search also looks at.
SEARCH_FIELDS = ('name', 'category') + PLACE_FIELDS


def _norm(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())


def _spellings_of(term):
    """Every spelling of the place `term` names (just `term` if unknown)."""
    for group in SPELLINGS:
        if term in group:
            return group
    return (term,)


def place_terms(term):
    """The strings that count as a match for the place `term`.

    'warangal'   -> warangal, orugallu, hanamkonda (all spellings), kazipet…
    'hanumakonda'-> hanamkonda, hanumakonda, hanmakonda, hanamakonda
    'gachibowli' -> gachibowli
    """
    term = _norm(term)
    if not term:
        return ()
    canonical = _spellings_of(term)[0]
    terms = []
    for place in AREAS.get(canonical, (canonical,)):
        terms.extend(_spellings_of(place))
    if term not in terms:
        terms.append(term)
    return tuple(dict.fromkeys(terms))


def place_q(term, fields=PLACE_FIELDS):
    """A Q matching events whose place fields mention the place `term`."""
    query = Q()
    for candidate in place_terms(term):
        for field in fields:
            query |= Q(**{f'{field}__icontains': candidate})
    return query


def search_q(text):
    """Free-text search: EVERY word must match some field, so
    'dandiya warangal' narrows to Dandiya nights in the Warangal area rather
    than widening to anything matching either word (the same rule the home
    page's venue search uses). Place words are alias-expanded."""
    query = Q()
    for token in _norm(text).split(' '):
        if token:
            query &= place_q(token, SEARCH_FIELDS)
    return query


def _segments(*parts):
    """Comma-separated segments of `parts`, de-duplicated case-insensitively,
    so an address that already ends in the city doesn't print it twice."""
    seen, out = set(), []
    for part in parts:
        for segment in str(part or '').split(','):
            segment = segment.strip()
            key = segment.lower()
            if segment and key not in seen:
                seen.add(key)
                out.append(segment)
    return out


def short_location(event):
    """The one-line card place: venue, locality, city — no street address."""
    return ', '.join(_segments(event.venue_name, event.locality, event.city))


def full_address(event):
    """Everything known about where it is: venue, address, locality, city."""
    return ', '.join(
        _segments(event.venue_name, event.address, event.locality, event.city)
    )


# google.com, google.co.in, google.com.au, google.de … — EXACTLY. A host that
# merely starts with "www.google." (www.google.com.evil.example) is not Google.
GOOGLE_HOST = re.compile(r'^(www\.|maps\.)?google\.(com|[a-z]{2}|co\.[a-z]{2}|com\.[a-z]{2})$')


def clean_maps_link(raw):
    """'' or a Google Maps URL. Raises SlotError for anything else.

    Validated server-side because the customer page embeds it in an iframe
    and links to it: an arbitrary URL there is an open redirect / phishing
    vector. Accepts full Google Maps URLs (any google.<tld>) and the share
    short links the venue resolver already trusts.
    """
    from venues.maps import _is_allowed

    link = str(raw or '').strip()
    if not link:
        return ''
    if len(link) > 500:
        raise SlotError('The Google Maps link is too long.')
    parsed = urlparse(link)
    host = (parsed.hostname or '').lower()
    is_maps = _is_allowed(link) or (
        parsed.scheme in ('http', 'https')
        and GOOGLE_HOST.match(host) is not None
        and (host.startswith('maps.') or parsed.path.startswith('/maps'))
    )
    if not is_maps:
        raise SlotError(
            'mapsLink must be a Google Maps link '
            '(e.g. https://maps.app.goo.gl/… or https://www.google.com/maps/…).'
        )
    return link
