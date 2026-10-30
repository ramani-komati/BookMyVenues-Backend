"""
Venue Timings — the opening hours a vendor (or a super admin) gives a venue
in the registration / edit form: one open time, one close time, or "open 24
hours". Every venue category has them except Events, whose dates and show
times are scheduled per event instead.

Stored in the listing record as `detail.timings` (and in the draft's
`details` as openTime / closeTime / open24Hours), because the record round-
trips through the draft: the vendor portal republishes the listing from the
draft, so anything kept only on the listing would be wiped.

    {"open": "09:00", "close": "22:00", "open24Hours": false}
    {"open": "18:00", "close": "02:00", "open24Hours": false}   closes after midnight
    {"open": "",      "close": "",      "open24Hours": true}

HOW THEY AFFECT BOOKINGS. The platform books on a 30-minute grid between
06:00 and midnight (bookings/slots.py). The timings become the venue's
DEFAULT open periods inside that grid (bookable_periods) — used only while
the vendor has not set detailed weekly hours under Business controls, which
are more specific and always win (venues/business.py open_periods). A venue
open 24 hours, or past midnight, is still bookable only within the grid;
customers see the real hours on the venue page.

Existing bookings are never touched by a timings change.
"""
import re

from bookings.slots import CLOSE_MINUTE, OPEN_MINUTE, STEP_MINUTES

_CLOCK_RE = re.compile(r'^(\d{1,2}):(\d{2})$')
DAY_MINUTES = 24 * 60


class TimingsError(ValueError):
    """A timings value that cannot be saved; the message is user-facing."""


def _minute(text, label):
    match = _CLOCK_RE.match(str(text or '').strip())
    if not match:
        raise TimingsError(f'{label}: enter a time like 09:00.')
    hours, minutes = int(match[1]), int(match[2])
    if hours > 23 or minutes > 59:
        raise TimingsError(f'{label}: {text} is not a valid time.')
    return hours * 60 + minutes


def _clock(minute):
    return f'{minute // 60:02d}:{minute % 60:02d}'


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


def from_draft(details):
    """The timings carried by a wizard `details` section (openTime /
    closeTime / open24Hours), in the record shape — or None when the draft
    has none yet."""
    details = details or {}
    if not any(k in details for k in ('openTime', 'closeTime', 'open24Hours')):
        return None
    return {
        'open': details.get('openTime') or '',
        'close': details.get('closeTime') or '',
        'open24Hours': details.get('open24Hours'),
    }


def validate_timings(raw):
    """The canonical timings dict, or None for "not set".

    Raises TimingsError for a value that is present but wrong — the same
    rules the forms enforce, checked again here because the server never
    trusts the form."""
    if raw in (None, '', {}):
        return None
    if not isinstance(raw, dict):
        raise TimingsError('Venue timings must be an object.')
    if _truthy(raw.get('open24Hours')):
        return {'open': '', 'close': '', 'open24Hours': True}
    open_text = str(raw.get('open') or '').strip()
    close_text = str(raw.get('close') or '').strip()
    if not open_text and not close_text:
        return None
    if not open_text:
        raise TimingsError('Venue timings: enter the opening time.')
    if not close_text:
        raise TimingsError('Venue timings: enter the closing time.')
    start = _minute(open_text, 'Open time')
    end = _minute(close_text, 'Close time')
    if start == end:
        raise TimingsError(
            'Venue timings: the opening and closing times are the same. '
            'Turn on "Open 24 hours" if the venue never closes.'
        )
    timings = {'open': _clock(start), 'close': _clock(end), 'open24Hours': False}
    if not bookable_periods(timings):
        raise TimingsError(
            'Venue timings: these hours leave no time customers can book — '
            'online bookings are taken between 6:00 AM and 12:00 AM (midnight).'
        )
    return timings


def is_overnight(timings):
    """True when the venue closes after midnight (close earlier than open)."""
    if not timings or timings.get('open24Hours'):
        return False
    try:
        return _minute(timings.get('close'), 'Close') < _minute(timings.get('open'), 'Open') \
            and _minute(timings.get('close'), 'Close') != 0
    except TimingsError:
        return False


def bookable_periods(timings):
    """The timings as open periods on the booking grid: sorted
    (start, end) minute pairs inside 06:00–24:00, on 30-minute steps.

    None / "open 24 hours" → the whole grid. A venue that closes after
    midnight is open from its opening time to midnight, plus — the night
    before running on — from 06:00 to its closing time when that is after
    06:00. Opening rounds UP and closing rounds DOWN to the grid, so a
    booking can never start before the venue opens or end after it closes."""
    if not timings or timings.get('open24Hours'):
        return [(OPEN_MINUTE, CLOSE_MINUTE)]
    try:
        start = _minute(timings.get('open'), 'Open')
        end = _minute(timings.get('close'), 'Close')
    except TimingsError:
        return [(OPEN_MINUTE, CLOSE_MINUTE)]
    if end == 0:
        end = DAY_MINUTES                       # closes at midnight
    up = -(-start // STEP_MINUTES) * STEP_MINUTES

    def down(minute):
        return minute // STEP_MINUTES * STEP_MINUTES

    raw = [(up, down(end))] if end > start else [(OPEN_MINUTE, down(end)), (up, DAY_MINUTES)]
    periods = []
    for s, e in raw:
        s, e = max(s, OPEN_MINUTE), min(e, CLOSE_MINUTE)
        if e > s:
            periods.append((s, e))
    return sorted(periods)


def _ampm(minute):
    m = minute % DAY_MINUTES
    hour = m // 60
    return f'{hour % 12 or 12}:{m % 60:02d} {"AM" if hour < 12 else "PM"}'


def timings_label(timings):
    """'Open 24 hours' / '9:00 AM – 10:00 PM' / '6:00 PM – 2:00 AM (next day)'."""
    if not timings:
        return ''
    if timings.get('open24Hours'):
        return 'Open 24 hours'
    try:
        start = _minute(timings.get('open'), 'Open')
        end = _minute(timings.get('close'), 'Close')
    except TimingsError:
        return ''
    suffix = ' (next day)' if end < start and end != 0 else ''
    return f'{_ampm(start)} – {_ampm(end)}{suffix}'


def listing_timings(listing):
    """The saved timings of a listing's BASE venue (pitch / screen listings
    share their base's hours), or None."""
    if listing is None:
        return None
    from .business import base_listing
    base = base_listing(listing)
    detail = (getattr(base, 'record', None) or {}).get('detail') or {}
    try:
        return validate_timings(detail.get('timings'))
    except TimingsError:
        return None
