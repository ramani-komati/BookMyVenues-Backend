"""
Vendor business controls — opening hours, closures, blocked slots and price
rules — and the ONE place that enforces them.

Storage: VenueBusinessSettings (one row per BASE venue) and SlotBlock rows.
A venue's pitch / screen listings (detail.unitOf) share their base venue's
row, the same way part payment is inherited (bookings/part_payment.py).

WHAT A CUSTOMER MAY BOOK on a date (check_bookable):
  1. a closure covering the date            → closed all day
  2. else a special-hours entry for the date → those periods only
  3. else the weekly hours for the weekday   → those periods only
  4. no weekly hours configured              → the Venue Timings saved with
                                               the venue (venues/timings.py),
                                               else 06:00–24:00 (the platform's
                                               long-standing default)
  and never a time a SlotBlock covers. Every booked range must sit inside ONE
  open period. Walk-ins (the vendor's own entries) skip the opening hours but
  still respect blocks — a vendor reopens a block first.

PRICE PRECEDENCE for each 30-minute step of a booking (rate_at):
  Each active rule that covers the step's date, weekday, time and resource
  scores   +4 if it is a DATE rule (selected dates / holidays)
           +2 if it has a TIME RANGE (otherwise it covers the whole day)
           +1 if it names SPECIFIC pitches/screens (otherwise every resource)
  The highest score wins. Two rules with the SAME score may never overlap —
  validate_price_rules refuses that, so the winner is never ambiguous. When no
  rule applies the venue's existing price is used: the pitch / venue rate,
  and its weekend price on Saturday and Sunday (bookings.views.base_rate /
  _unit_rate), exactly as before.

  The slot charge is round(Σ step rate × 30 / 60). With no rules that is the
  old round(rate × minutes / 60) to the rupee, so existing pricing is
  unchanged.

EXISTING BOOKINGS are never touched: a booking stores its amount when it is
created, and nothing here rewrites a booking row. A schedule change that
would leave a booking outside the new hours is refused until the vendor
acknowledges each such booking (it stays confirmed and must be honoured);
a block over a booking is refused outright.
"""
import datetime
import re
import uuid

from django.db import transaction
from django.utils import timezone

from bookings.slots import (
    CLOSE_MINUTE,
    OPEN_MINUTE,
    STEP_MINUTES,
    SlotError,
    now_minutes_ist,
    overlaps,
    parse_slots,
    today_ist,
)

from .models import Listing, SlotBlock, VenueBusinessSettings

DAY_KEYS = tuple(str(i) for i in range(7))          # Monday = "0"
DAY_NAMES = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')

MAX_PERIODS_PER_DAY = 4
MAX_SPECIAL_DAYS = 120
MAX_CLOSURES = 60
MAX_RULES = 60
MAX_RULE_SPAN_DAYS = 366
MAX_CLOSURE_SPAN_DAYS = 366
MAX_PRICE = 1_000_000
MAX_LABEL = 60
MAX_REASON = 200
MAX_INSTRUCTIONS = 1000
MAX_BLOCK_DAYS_AHEAD = 365
MAX_CALENDAR_DAYS = 7

RULE_KINDS = ('weekly', 'dates')

_CLOCK_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*$')


class BusinessError(ValueError):
    """A vendor input the rules refuse — always with a message a
    non-technical vendor can act on."""

    def __init__(self, message, code='INVALID'):
        super().__init__(message)
        self.code = code


class StaleRevision(Exception):
    """The settings changed since the vendor loaded them (another tab)."""


class Conflicts(Exception):
    """A change that would affect existing bookings, with the bookings."""

    def __init__(self, bookings, message):
        super().__init__(message)
        self.bookings = bookings
        self.message = message


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------

def clock(minute):
    """540 -> '09:00'; the end of day is written '24:00'."""
    return f'{minute // 60:02d}:{minute % 60:02d}'


def ampm(minute):
    """540 -> '9:00 AM'; 1440 -> '12:00 AM' (midnight)."""
    m = minute % 1440
    hour = m // 60
    h12 = hour % 12 or 12
    return f'{h12}:{m % 60:02d} {"AM" if hour < 12 else "PM"}'


def parse_clock(text, *, label, is_end=False):
    """'09:30' -> 570. An END of '00:00' or '24:00' means midnight (1440).
    Times must sit on the booking grid (30 minutes) inside the hours the
    platform can book at all (06:00–24:00)."""
    match = _CLOCK_RE.match(str(text or ''))
    if not match:
        raise BusinessError(f'{label}: enter a time like 09:00.')
    hours, minutes = int(match[1]), int(match[2])
    if minutes >= 60 or hours > 24 or (hours == 24 and minutes):
        raise BusinessError(f'{label}: {text} is not a valid time.')
    total = hours * 60 + minutes
    if is_end and total == 0:
        total = CLOSE_MINUTE
    if total % STEP_MINUTES:
        raise BusinessError(f'{label}: times must be on the hour or half hour.')
    if total < OPEN_MINUTE or total > CLOSE_MINUTE:
        raise BusinessError(
            f'{label}: bookings can only be taken between 6:00 AM and 12:00 AM (midnight).'
        )
    return total


def parse_day(text, *, label):
    try:
        return datetime.date.fromisoformat(str(text or '').strip())
    except ValueError:
        raise BusinessError(f'{label}: pick a valid date.')


def nice_date(day):
    return day.strftime('%a, %d %b %Y')


def _text(value, *, label, limit, required=False):
    text = str(value or '').strip()
    if required and not text:
        raise BusinessError(f'{label} is required.')
    if len(text) > limit:
        raise BusinessError(f'{label} can be at most {limit} characters.')
    return text


def _price(value, *, label):
    raw = str(value if value is not None else '').strip().replace(',', '')
    try:
        number = int(raw)
    except ValueError:
        try:
            as_float = float(raw)
        except ValueError:
            raise BusinessError(f'{label}: enter the price in whole rupees.')
        if as_float != int(as_float):
            raise BusinessError(f'{label}: enter the price in whole rupees.')
        number = int(as_float)
    if number <= 0:
        raise BusinessError(f'{label}: the price must be more than ₹0.')
    if number > MAX_PRICE:
        raise BusinessError(f'{label}: that price is too high.')
    return number


# ---------------------------------------------------------------------------
# The venue set (base venue + its pitch / screen listings)
# ---------------------------------------------------------------------------

def base_listing(listing):
    """The venue a pitch / screen listing belongs to — itself for a base.
    `unitOf` is validated at publish time to name the SAME vendor's venue;
    the vendor check is repeated so a stray value can never borrow another
    vendor's settings."""
    detail = (listing.record or {}).get('detail') or {}
    ref = str(detail.get('unitOf') or '').strip() if isinstance(detail, dict) else ''
    if not ref or ref == str(listing.pk):
        return listing
    try:
        base = Listing.objects.filter(pk=uuid.UUID(ref)).first()
    except (ValueError, TypeError):
        return listing
    if base is None or base.vendor_id != listing.vendor_id:
        return listing
    return base


def venue_set(base):
    """[base, *its unit listings] — the resources one settings row covers.
    Deleted siblings are left out: they take no bookings."""
    siblings = list(
        Listing.objects.filter(
            record__detail__unitOf=str(base.pk), vendor_id=base.vendor_id,
        ).exclude(pk=base.pk).exclude(status=Listing.Status.DELETED).order_by('name')
    )
    return [base, *siblings]


_UNIT_LABEL_RE = re.compile(r'^(?:(?P<sport>.+?) · )?(?P<noun>[A-Z][a-z]+) (?P<n>\d+)$')


def resource_label(listing, siblings=()):
    """A short name for one bookable resource: 'Pitch 2', 'Screen 1'.

    Unit listings carry their own label (detail.unitLabel, else the part of
    the name after ' — '). The base venue IS resource #1; it carries no label
    of its own, so one is derived from its siblings ('Pitch 2' → 'Pitch 1').
    A venue without siblings is simply 'Whole venue'.
    """
    detail = (listing.record or {}).get('detail') or {}
    own = str(detail.get('unitLabel') or '').strip() if isinstance(detail, dict) else ''
    if own:
        return own
    name = listing.name or ''
    if ' — ' in name and str(detail.get('unitOf') or '').strip():
        return name.rsplit(' — ', 1)[1]
    others = [s for s in siblings if s.pk != listing.pk]
    if not others:
        return 'Whole venue'
    sports = [s for s in (detail.get('sports') or []) if isinstance(s, dict) and s.get('name')]
    first_sport = str(sports[0].get('name')) if sports else ''
    for other in others:
        match = _UNIT_LABEL_RE.match(resource_label(other))
        if match and (not match['sport'] or match['sport'] == first_sport):
            prefix = f'{match["sport"]} · ' if match['sport'] else ''
            return f'{prefix}{match["noun"]} 1'
    return first_sport or 'Main'


def settings_for(listing):
    """The settings row that governs this listing, or None (= defaults)."""
    base = base_listing(listing)
    return VenueBusinessSettings.objects.filter(listing=base).first()


# ---------------------------------------------------------------------------
# Validation — every vendor write goes through these
# ---------------------------------------------------------------------------

def _periods(raw, *, label):
    """A day's opening periods: sorted, non-overlapping (start, end) pairs."""
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise BusinessError(f'{label}: opening times must be a list.')
    if len(raw) > MAX_PERIODS_PER_DAY:
        raise BusinessError(f'{label}: at most {MAX_PERIODS_PER_DAY} opening periods a day.')
    out = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise BusinessError(f'{label}: period {index} is not valid.')
        where = f'{label}, period {index}'
        start = parse_clock(entry.get('start'), label=where)
        end = parse_clock(entry.get('end'), label=where, is_end=True)
        if end <= start:
            raise BusinessError(f'{where}: closing time must be after opening time.')
        out.append((start, end))
    out.sort()
    for previous, current in zip(out, out[1:]):
        if current[0] < previous[1]:
            raise BusinessError(
                f'{label}: {ampm(previous[0])}–{ampm(previous[1])} and '
                f'{ampm(current[0])}–{ampm(current[1])} overlap. Merge them into one period.'
            )
    return out


def _periods_json(periods):
    return [{'start': clock(s), 'end': clock(e)} for s, e in periods]


def validate_hours(raw):
    """Weekly hours → {"0": [...], …, "6": [...]}, or None for the default.
    Every weekday must be present: a missing day is refused rather than
    silently treated as closed (or open)."""
    if raw is None:
        return None
    if isinstance(raw, list) and len(raw) == 7:
        raw = {str(i): day for i, day in enumerate(raw)}
    if not isinstance(raw, dict):
        raise BusinessError('Weekly hours must list all seven days.')
    missing = [DAY_NAMES[int(k)] for k in DAY_KEYS if k not in raw]
    if missing:
        raise BusinessError(f'Set hours for every day (or mark it closed): {", ".join(missing)}.')
    return {
        key: _periods_json(_periods(raw.get(key), label=DAY_NAMES[int(key)]))
        for key in DAY_KEYS
    }


def validate_special_hours(raw):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise BusinessError('Special dates must be a list.')
    if len(raw) > MAX_SPECIAL_DAYS:
        raise BusinessError(f'At most {MAX_SPECIAL_DAYS} special dates.')
    out = {}
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise BusinessError(f'Special date {index} is not valid.')
        day = parse_day(entry.get('date'), label=f'Special date {index}')
        label = f'Special hours on {nice_date(day)}'
        if day.isoformat() in out:
            raise BusinessError(f'{nice_date(day)} is listed twice in special dates.')
        periods = _periods(entry.get('periods'), label=label)
        out[day.isoformat()] = {
            'date': day.isoformat(),
            'periods': _periods_json(periods),
            'note': _text(entry.get('note'), label=f'{label} note', limit=MAX_LABEL),
        }
    return [out[key] for key in sorted(out)]


def validate_closures(raw):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise BusinessError('Closures must be a list.')
    if len(raw) > MAX_CLOSURES:
        raise BusinessError(f'At most {MAX_CLOSURES} closures.')
    out = []
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise BusinessError(f'Closure {index} is not valid.')
        start = parse_day(entry.get('from'), label=f'Closure {index} start')
        end = parse_day(entry.get('to') or entry.get('from'), label=f'Closure {index} end')
        if end < start:
            raise BusinessError(f'Closure {index}: the end date is before the start date.')
        if (end - start).days >= MAX_CLOSURE_SPAN_DAYS:
            raise BusinessError(f'Closure {index}: a closure can be at most a year long.')
        out.append({
            'from': start.isoformat(),
            'to': end.isoformat(),
            'reason': _text(entry.get('reason'), label=f'Closure {index} reason',
                            limit=MAX_REASON, required=True),
        })
    out.sort(key=lambda c: (c['from'], c['to']))
    return out


def _resources(raw, allowed, *, label):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise BusinessError(f'{label}: choose the pitches/screens from the list.')
    out = []
    for value in raw:
        text = str(value or '').strip()
        if text not in allowed:
            raise BusinessError(f'{label}: that pitch/screen is not part of this venue.')
        if text not in out:
            out.append(text)
    # Naming every resource is the same as naming none — store it as "all"
    # so the rule's precedence matches what the vendor sees.
    return [] if set(out) == set(allowed) else out


def specificity(rule):
    return ((4 if rule['kind'] == 'dates' else 0)
            + (2 if rule.get('start') else 0)
            + (1 if rule.get('resources') else 0))


def _rule_name(rule):
    return f'“{rule["label"]}”' if rule.get('label') else 'a rule'


def _rules_overlap(a, b):
    """Could rules a and b ever price the same step of the same resource?"""
    if a['kind'] != b['kind']:
        return False
    if a['kind'] == 'weekly':
        if not set(a['days']) & set(b['days']):
            return False
    elif a['to'] < b['from'] or b['to'] < a['from']:
        return False
    a_start = parse_clock(a['start'], label='') if a.get('start') else OPEN_MINUTE
    a_end = parse_clock(a['end'], label='', is_end=True) if a.get('end') else CLOSE_MINUTE
    b_start = parse_clock(b['start'], label='') if b.get('start') else OPEN_MINUTE
    b_end = parse_clock(b['end'], label='', is_end=True) if b.get('end') else CLOSE_MINUTE
    if not (a_start < b_end and b_start < a_end):
        return False
    a_res, b_res = set(a.get('resources') or []), set(b.get('resources') or [])
    return not a_res or not b_res or bool(a_res & b_res)


def validate_price_rules(raw, resource_ids):
    """Normalise the vendor's price rules and refuse ambiguous ones.

    A rule: {id?, label, kind: 'weekly'|'dates', days: [0-6] (weekly),
    from/to: 'YYYY-MM-DD' (dates), start/end: 'HH:MM' (both, or neither =
    all day), price: ₹ per hour, resources: [listing ids] (empty = all),
    active: bool}. Two ACTIVE rules of the same specificity (see the module
    docstring) that could price the same step are refused, naming both.
    """
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise BusinessError('Price rules must be a list.')
    if len(raw) > MAX_RULES:
        raise BusinessError(f'At most {MAX_RULES} price rules per venue.')
    allowed = [str(r) for r in resource_ids]
    rules = []
    seen_ids = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise BusinessError(f'Price rule {index} is not valid.')
        label = _text(entry.get('label'), label=f'Price rule {index} name', limit=MAX_LABEL)
        where = f'“{label}”' if label else f'Price rule {index}'
        kind = str(entry.get('kind') or '').strip()
        if kind not in RULE_KINDS:
            raise BusinessError(f'{where}: choose days of the week or specific dates.')
        rule = {
            'id': str(entry.get('id') or '').strip()[:40] or uuid.uuid4().hex[:12],
            'label': label,
            'kind': kind,
            'price': _price(entry.get('price'), label=where),
            'active': entry.get('active', True) is not False,
        }
        if rule['id'] in seen_ids:
            rule['id'] = uuid.uuid4().hex[:12]
        seen_ids.add(rule['id'])
        if kind == 'weekly':
            days = entry.get('days')
            if not isinstance(days, list) or not days:
                raise BusinessError(f'{where}: pick at least one day of the week.')
            clean = set()
            for day in days:
                try:
                    number = int(day)
                except (TypeError, ValueError):
                    raise BusinessError(f'{where}: {day} is not a day of the week.')
                if not 0 <= number <= 6:
                    raise BusinessError(f'{where}: {day} is not a day of the week.')
                clean.add(number)
            rule['days'] = sorted(clean)
        else:
            start = parse_day(entry.get('from'), label=f'{where} start date')
            end = parse_day(entry.get('to') or entry.get('from'), label=f'{where} end date')
            if end < start:
                raise BusinessError(f'{where}: the end date is before the start date.')
            if (end - start).days >= MAX_RULE_SPAN_DAYS:
                raise BusinessError(f'{where}: a date rule can cover at most a year.')
            rule['from'], rule['to'] = start.isoformat(), end.isoformat()
        has_start = str(entry.get('start') or '').strip()
        has_end = str(entry.get('end') or '').strip()
        if has_start or has_end:
            if not (has_start and has_end):
                raise BusinessError(f'{where}: set both a start and an end time, or neither for all day.')
            start_min = parse_clock(has_start, label=where)
            end_min = parse_clock(has_end, label=where, is_end=True)
            if end_min <= start_min:
                raise BusinessError(f'{where}: the end time must be after the start time.')
            if (start_min, end_min) == (OPEN_MINUTE, CLOSE_MINUTE):
                rule['start'] = rule['end'] = None      # = all day
            else:
                rule['start'], rule['end'] = clock(start_min), clock(end_min)
        else:
            rule['start'] = rule['end'] = None
        rule['resources'] = _resources(entry.get('resources'), allowed, label=where)
        rules.append(rule)

    active = [r for r in rules if r['active']]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if specificity(a) == specificity(b) and _rules_overlap(a, b):
                both = (
                    f'The price rules {_rule_name(a)} and {_rule_name(b)}'
                    if a.get('label') and b.get('label') else 'Two of your price rules'
                )
                raise BusinessError(
                    f'{both} overlap — the same '
                    f'time would have two prices. Change the days, dates, times or '
                    f'pitches so they don’t overlap, or switch one off.',
                    code='OVERLAPPING_RULES',
                )
    return rules


# ---------------------------------------------------------------------------
# The effective schedule on a date
# ---------------------------------------------------------------------------

def _merge(periods):
    """Join touching periods: 09:00–13:00 + 13:00–22:00 is one stretch."""
    merged = []
    for start, end in sorted(periods):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _stored_periods(entries):
    out = []
    for entry in entries or []:
        try:
            out.append((parse_clock(entry.get('start'), label=''),
                        parse_clock(entry.get('end'), label='', is_end=True)))
        except (BusinessError, AttributeError):
            continue          # a malformed stored row must never crash a read
    return out


def closure_on(settings, day):
    """The closure covering `day`, or None."""
    if settings is None:
        return None
    iso = day.isoformat()
    for closure in settings.closures or []:
        if isinstance(closure, dict) and closure.get('from', '') <= iso <= closure.get('to', ''):
            return closure
    return None


def special_on(settings, day):
    if settings is None:
        return None
    iso = day.isoformat()
    for entry in settings.special_hours or []:
        if isinstance(entry, dict) and entry.get('date') == iso:
            return entry
    return None


def default_periods(listing):
    """The open periods a venue has when no weekly hours are configured:
    its saved Venue Timings on the booking grid, else 06:00–24:00."""
    from .timings import bookable_periods, listing_timings
    return bookable_periods(listing_timings(listing))


def open_periods(settings, day, listing=None):
    """(merged open periods, why) for `day`. `why` is None on an ordinary
    day, else {'kind': 'closure'|'special'|'weekly', 'note': ...} explaining
    a closed or changed day.

    Without weekly hours the venue's saved Venue Timings apply (`listing`,
    else the settings row's own venue); weekly hours, special dates and
    closures are more specific and always win."""
    if listing is None and settings is not None:
        listing = settings.listing
    if settings is None:
        return default_periods(listing), None
    closure = closure_on(settings, day)
    if closure is not None:
        return [], {'kind': 'closure', 'note': closure.get('reason') or ''}
    special = special_on(settings, day)
    if special is not None:
        periods = _merge(_stored_periods(special.get('periods')))
        return periods, {'kind': 'special', 'note': special.get('note') or ''}
    if settings.hours is None:
        return default_periods(listing), None
    periods = _merge(_stored_periods((settings.hours or {}).get(str(day.weekday()))))
    return periods, (None if periods else {'kind': 'weekly', 'note': ''})


def closed_ranges(periods):
    """The gaps between open periods inside 06:00–24:00."""
    gaps, cursor = [], OPEN_MINUTE
    for start, end in periods:
        if start > cursor:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < CLOSE_MINUTE:
        gaps.append((cursor, CLOSE_MINUTE))
    return gaps


def blocks_for(listing, day, base=None):
    """The SlotBlock rows covering this listing on `day`."""
    base = base or base_listing(listing)
    own = str(listing.pk)
    return [
        block for block in SlotBlock.objects.filter(listing=base, date=day)
        if not block.resources or own in block.resources
    ]


def unavailable_ranges(listing, day, *, settings=None, include_private=False):
    """Every range on `day` this listing cannot take a booking for, apart
    from existing bookings: outside the opening hours and blocked.
    Public callers get only the time and a reason code; the vendor's own
    calendar (include_private) also gets the block id and its note."""
    base = base_listing(listing)
    if settings is None:
        settings = VenueBusinessSettings.objects.filter(listing=base).first()
    periods, why = open_periods(settings, day, listing=base)
    out = []
    for start, end in closed_ranges(periods):
        row = {'start': clock(start), 'end': clock(end), 'reason': 'closed'}
        if why and why.get('note') and why['kind'] == 'closure':
            row['note'] = why['note']       # closures are customer-facing
        out.append(row)
    for block in blocks_for(listing, day, base):
        row = {'start': clock(block.start_minute), 'end': clock(block.end_minute),
               'reason': 'blocked'}
        if include_private:
            row['id'] = block.pk
            row['note'] = block.reason
        out.append(row)
    out.sort(key=lambda r: r['start'])
    return out


def _fits(intervals, periods):
    return all(any(ps <= s and e <= pe for ps, pe in periods) for s, e in intervals)


def check_bookable(listing, day, intervals, *, enforce_hours=True, settings=None):
    """Raise SlotError (with a `code`) when `intervals` on `day` fall outside
    the venue's opening hours or on a blocked range. Customers get the hours
    check; walk-ins (enforce_hours=False) only the blocks."""
    base = base_listing(listing)
    if settings is None:
        settings = VenueBusinessSettings.objects.filter(listing=base).first()
    if enforce_hours:
        periods, why = open_periods(settings, day, listing=base)
        if not periods:
            reason = (why or {}).get('note') if (why or {}).get('kind') == 'closure' else ''
            raise SlotError(
                f'This venue is closed on {nice_date(day)}'
                f'{f" — {reason}" if reason else ""}. Please pick another date.',
                code='VENUE_CLOSED',
            )
        if not _fits(intervals, periods):
            hours = ', '.join(f'{ampm(s)} – {ampm(e)}' for s, e in periods)
            raise SlotError(
                f'That time is outside the venue’s opening hours on {nice_date(day)} '
                f'({hours}). Please pick a time within them.',
                code='OUTSIDE_HOURS',
            )
    blocked = [(b.start_minute, b.end_minute) for b in blocks_for(listing, day, base)]
    if blocked and overlaps(intervals, blocked):
        raise SlotError(
            'That time is not available at this venue. Please pick another time.',
            code='SLOT_BLOCKED',
        )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def _active_rules(settings):
    if settings is None:
        return []
    return [r for r in (settings.price_rules or []) if isinstance(r, dict) and r.get('active', True)]


def _rule_window(rule):
    if rule.get('start') and rule.get('end'):
        try:
            return (parse_clock(rule['start'], label=''),
                    parse_clock(rule['end'], label='', is_end=True))
        except BusinessError:
            return None
    return OPEN_MINUTE, CLOSE_MINUTE


def _rule_covers_day(rule, listing_id, day):
    resources = rule.get('resources') or []
    if resources and listing_id not in resources:
        return False
    if rule.get('kind') == 'dates':
        return str(rule.get('from') or '') <= day.isoformat() <= str(rule.get('to') or '')
    if rule.get('kind') == 'weekly':
        return day.weekday() in (rule.get('days') or [])
    return False


def rules_for_day(settings, listing, day):
    """The active rules that could price `listing` on `day`, with their
    windows, highest precedence first."""
    listing_id = str(listing.pk)
    out = []
    for rule in _active_rules(settings):
        if not _rule_covers_day(rule, listing_id, day):
            continue
        window = _rule_window(rule)
        if window is None:
            continue
        try:
            price = int(rule.get('price'))
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        out.append((specificity(rule), window, price, rule))
    out.sort(key=lambda item: -item[0])
    return out


def rate_at(day_rules, minute, default_rate):
    """(rate, rule) for the 30-minute step starting at `minute`."""
    for _score, (start, end), price, rule in day_rules:
        if start <= minute < end:
            return price, rule
    return default_rate, None


def slot_charge(listing, day, intervals, default_rate, *, settings=None):
    """The hourly charge for `intervals`, every step priced by the rules.
    Identical to round(rate × minutes / 60) when no rule applies."""
    if settings is None:
        settings = settings_for(listing)
    day_rules = rules_for_day(settings, listing, day) if settings else []
    minutes = sum(end - start for start, end in intervals)
    if not day_rules:
        return round(default_rate * minutes / 60)
    total = 0
    for start, end in intervals:
        for minute in range(start, end, STEP_MINUTES):
            rate, _rule = rate_at(day_rules, minute, default_rate)
            total += rate * min(STEP_MINUTES, end - minute)
    return round(total / 60)


def price_bands(listing, day, *, settings=None, default_rate=None):
    """The day split into stretches of one price, for customers and the
    vendor's preview: [{start, end, rate, label}]. `rate` is None where the
    venue's own (default) price applies — the caller knows that rate, which
    can differ per pitch — unless `default_rate` is given."""
    if settings is None:
        settings = settings_for(listing)
    day_rules = rules_for_day(settings, listing, day) if settings else []
    bands = []
    for minute in range(OPEN_MINUTE, CLOSE_MINUTE, STEP_MINUTES):
        rate, rule = rate_at(day_rules, minute, default_rate)
        label = (rule or {}).get('label') or ('' if rule is None else 'Special price')
        key = (rate, label, (rule or {}).get('id'))
        if bands and bands[-1]['_key'] == key:
            bands[-1]['end'] = clock(minute + STEP_MINUTES)
        else:
            bands.append({
                '_key': key, 'start': clock(minute), 'end': clock(minute + STEP_MINUTES),
                'rate': rate, 'label': label or None, 'ruleId': (rule or {}).get('id'),
            })
    for band in bands:
        band.pop('_key')
    return bands


# ---------------------------------------------------------------------------
# Bookings affected by a change
# ---------------------------------------------------------------------------

def upcoming_bookings(listings):
    """Active bookings (confirmed, or a fresh payment hold) on these
    listings that have not ended yet — what a schedule change could hit."""
    from bookings.slots import slots_end_minute
    from bookings.holds import expired_hold_q
    from bookings.views import exclude_dead
    from bookings.models import Booking

    today = today_ist()
    # A live 5-minute checkout hold counts; a lapsed one does not
    # (bookings/holds.py — the same rule availability uses).
    rows = exclude_dead(
        Booking.objects.filter(listing__in=listings, date__gte=today)
    ).exclude(expired_hold_q()).select_related('listing')
    now = now_minutes_ist()
    out = []
    for booking in rows:
        if booking.date == today and slots_end_minute(booking.slots) <= now:
            continue          # already over
        try:
            intervals = parse_slots(booking.slots)
        except SlotError:
            continue
        out.append((booking, intervals))
    return out


def booking_brief(booking, labels=None):
    return {
        'id': booking.id,
        'date': booking.date.isoformat(),
        'slots': booking.slots,
        'resource': (labels or {}).get(str(booking.listing_id)) or booking.unit_label or None,
        'customer': booking.customer_name or ('Walk-in' if booking.walk_in else 'Guest'),
        'status': booking.display_status,
        'amount': booking.amount,
    }


def hours_conflicts(candidate, listings, labels=None):
    """Bookings that would fall outside `candidate`'s schedule. Walk-ins are
    included: the vendor still has to honour them."""
    out = []
    for booking, intervals in upcoming_bookings(listings):
        periods, _why = open_periods(candidate, booking.date)
        if not _fits(intervals, periods):
            out.append(booking_brief(booking, labels))
    out.sort(key=lambda b: (b['date'], b['slots'][0] if b['slots'] else ''))
    return out


# ---------------------------------------------------------------------------
# Reads and writes for the vendor API
# ---------------------------------------------------------------------------

def _lock_set(base):
    """Lock every listing of the venue (pk order — the same order everywhere,
    so two saves can never deadlock). Booking creation locks the one listing
    it books, so a save and a booking for the same resource serialise."""
    listings = venue_set(base)
    ids = sorted(str(listing.pk) for listing in listings)
    list(Listing.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
    return listings


def labels_for(listings):
    return {str(l.pk): resource_label(l, listings) for l in listings}


def settings_payload(base, settings=None, listings=None):
    from bookings.views import base_rate

    listings = listings if listings is not None else venue_set(base)
    if settings is None:
        settings = VenueBusinessSettings.objects.filter(listing=base).first()
    labels = labels_for(listings)
    resources = []
    for listing in listings:
        try:
            weekday = base_rate(listing, False)
            weekend = base_rate(listing, True)
        except SlotError:
            weekday = weekend = None
        resources.append({
            'id': str(listing.pk), 'label': labels[str(listing.pk)], 'name': listing.name,
            'defaultRate': weekday, 'weekendRate': weekend,
        })
    return {
        'venue': {'id': str(base.pk), 'name': base.name, 'status': base.status},
        'resources': resources,
        'hours': settings.hours if settings else None,
        'usesDefaultHours': settings is None or settings.hours is None,
        # What "default" means for this venue: its saved Venue Timings
        # (null = never set → 06:00–24:00). Additive.
        **_timings_fields(base),
        'specialHours': list(settings.special_hours or []) if settings else [],
        'closures': list(settings.closures or []) if settings else [],
        'priceRules': list(settings.price_rules or []) if settings else [],
        'bookingInstructions': settings.booking_instructions if settings else '',
        'revision': settings.revision if settings else 0,
        'updatedAt': settings.updated_at.isoformat() if settings else None,
        'limits': {
            'open': clock(OPEN_MINUTE), 'close': clock(CLOSE_MINUTE), 'step': STEP_MINUTES,
            'maxPeriodsPerDay': MAX_PERIODS_PER_DAY, 'maxRules': MAX_RULES,
        },
    }


def _timings_fields(base):
    from .timings import bookable_periods, listing_timings, timings_label
    timings = listing_timings(base)
    return {
        'venueTimings': timings,
        'venueTimingsLabel': timings_label(timings),
        'defaultPeriods': [{'start': clock(s), 'end': clock(e)} for s, e in bookable_periods(timings)],
    }


def _check_revision(settings, body):
    current = settings.revision if settings else 0
    try:
        sent = int(body.get('revision'))
    except (TypeError, ValueError):
        raise BusinessError('Reload the page and try again (missing revision).')
    if sent != current:
        raise StaleRevision()


def _row(base, settings):
    return settings or VenueBusinessSettings(listing=base)


def _saved(row, user):
    row.revision = (row.revision or 0) + 1
    row.updated_by = user if getattr(user, 'pk', None) else None
    row.save()
    from .public_views import invalidate_listing_cache
    for listing in venue_set(row.listing):
        invalidate_listing_cache(listing)


def save_schedule(base, user, body, *, dry_run=False):
    """Weekly hours, special dates, closures and booking instructions.

    Bookings that would end up outside the new schedule are returned as a
    Conflicts error unless every one of them is listed in
    `acknowledgeConflicts` — they then stay confirmed (never cancelled or
    changed) and the vendor has said they will honour them.
    """
    hours = validate_hours(body.get('hours')) if 'hours' in body else ...
    special = validate_special_hours(body.get('specialHours')) if 'specialHours' in body else ...
    closures = validate_closures(body.get('closures')) if 'closures' in body else ...
    instructions = (
        _text(body.get('bookingInstructions'), label='Booking instructions', limit=MAX_INSTRUCTIONS)
        if 'bookingInstructions' in body else ...
    )
    acknowledged = {str(x) for x in (body.get('acknowledgeConflicts') or []) if x}

    with transaction.atomic():
        listings = _lock_set(base)
        settings = VenueBusinessSettings.objects.filter(listing=base).first()
        _check_revision(settings, body)
        row = _row(base, settings)
        if hours is not ...:
            row.hours = hours
        if special is not ...:
            row.special_hours = special
        if closures is not ...:
            row.closures = closures
        if instructions is not ...:
            row.booking_instructions = instructions
        conflicts = hours_conflicts(row, listings, labels_for(listings))
        if dry_run:
            transaction.set_rollback(True)
            return {'conflicts': conflicts}
        unacknowledged = [c for c in conflicts if c['id'] not in acknowledged]
        if unacknowledged:
            raise Conflicts(
                conflicts,
                f'{len(conflicts)} existing booking{"s" if len(conflicts) != 1 else ""} '
                f'fall{"" if len(conflicts) != 1 else "s"} outside the new hours. They stay '
                f'confirmed — confirm you will honour them, or keep the old hours.',
            )
        _saved(row, user)
        return {'conflicts': conflicts, 'settings': settings_payload(base, row, listings)}


def save_price_rules(base, user, body):
    with transaction.atomic():
        listings = _lock_set(base)
        settings = VenueBusinessSettings.objects.filter(listing=base).first()
        _check_revision(settings, body)
        rules = validate_price_rules(body.get('priceRules'), [l.pk for l in listings])
        row = _row(base, settings)
        row.price_rules = rules
        _saved(row, user)
        return settings_payload(base, row, listings)


def parse_block(body, listings):
    day = parse_day(body.get('date'), label='Date')
    start = parse_clock(body.get('start'), label='Start')
    end = parse_clock(body.get('end'), label='End', is_end=True)
    if end <= start:
        raise BusinessError('The end time must be after the start time.')
    today = today_ist()
    if day < today or (day == today and start < now_minutes_ist()):
        raise BusinessError('You can only block times that haven’t started yet.')
    if (day - today).days > MAX_BLOCK_DAYS_AHEAD:
        raise BusinessError('You can block times up to a year ahead.')
    resources = _resources(body.get('resources'), [str(l.pk) for l in listings], label='Block')
    reason = _text(body.get('reason'), label='Reason', limit=MAX_REASON)
    return day, start, end, resources, reason


def create_block(base, user, body):
    """Take a time range off sale. Refused — never forced — when it would
    cover an existing booking or another block."""
    with transaction.atomic():
        listings = _lock_set(base)
        day, start, end, resources, reason = parse_block(body, listings)
        covered = [l for l in listings if not resources or str(l.pk) in resources]
        labels = labels_for(listings)
        clashes = []
        for booking, intervals in upcoming_bookings(covered):
            if booking.date == day and overlaps(intervals, [(start, end)]):
                clashes.append(booking_brief(booking, labels))
        if clashes:
            raise Conflicts(
                clashes,
                'This time already has a booking, so it can’t be blocked. '
                'Pick a time around it.',
            )
        for existing in SlotBlock.objects.filter(listing=base, date=day):
            shared = (not existing.resources or not resources
                      or set(existing.resources) & set(resources))
            if shared and existing.start_minute < end and start < existing.end_minute:
                raise BusinessError(
                    f'{ampm(existing.start_minute)} – {ampm(existing.end_minute)} is already '
                    f'blocked on this date.', code='ALREADY_BLOCKED',
                )
        block = SlotBlock.objects.create(
            listing=base, resources=resources, date=day,
            start_minute=start, end_minute=end, reason=reason,
            created_by=user if getattr(user, 'pk', None) else None,
        )
        return block


def block_record(block, labels=None):
    return {
        'id': block.pk,
        'date': block.date.isoformat(),
        'start': clock(block.start_minute),
        'end': clock(block.end_minute),
        'resources': list(block.resources or []),
        'resourceLabels': [(labels or {}).get(r, r) for r in block.resources or []],
        'reason': block.reason,
        'createdAt': block.created_at.isoformat() if block.created_at else None,
    }


def delete_block(base, block_id):
    """Reopen a blocked range. A block whose time has already passed is
    history and stays."""
    with transaction.atomic():
        _lock_set(base)
        block = SlotBlock.objects.filter(listing=base, pk=block_id).first()
        if block is None:
            return None
        today = today_ist()
        if block.date < today or (block.date == today and block.end_minute <= now_minutes_ist()):
            raise BusinessError('This block is in the past and can’t be reopened.')
        block.delete()
        return block_id


def calendar(base, start_day, days):
    """The vendor's day-by-day view: per resource, every booking, block and
    closed range, plus the price bands — one read for the calendar screen."""
    from bookings.models import Booking
    from bookings.views import _active_bookings, _rates_for

    listings = venue_set(base)
    labels = labels_for(listings)
    settings = VenueBusinessSettings.objects.filter(listing=base).first()
    out_days = []
    for offset in range(days):
        day = start_day + datetime.timedelta(days=offset)
        periods, why = open_periods(settings, day, listing=base)
        resources = []
        for listing in listings:
            bookings = [
                {
                    **booking_brief(booking, labels),
                    'walkIn': booking.walk_in or booking.method == Booking.Method.WALK_IN,
                    'checkedIn': bool(booking.checked_in_at),
                    'pending': booking.status == 'payment_pending',
                }
                for booking in _active_bookings(listing, day).order_by('created_at')
            ]
            try:
                default = _rates_for(listing, day)['rate']
            except SlotError:
                default = None
            resources.append({
                'id': str(listing.pk),
                'label': labels[str(listing.pk)],
                'bookings': bookings,
                'unavailable': unavailable_ranges(listing, day, settings=settings,
                                                  include_private=True),
                'priceBands': price_bands(listing, day, settings=settings, default_rate=default),
                'defaultRate': default,
            })
        out_days.append({
            'date': day.isoformat(),
            'open': [{'start': clock(s), 'end': clock(e)} for s, e in periods],
            'closedReason': (why or {}).get('note') if (why or {}).get('kind') == 'closure' else None,
            'special': (why or {}).get('kind') == 'special',
            'resources': resources,
        })
    return {
        'venue': {'id': str(base.pk), 'name': base.name, 'status': base.status},
        'days': out_days,
        'blocks': [
            block_record(b, labels)
            for b in SlotBlock.objects.filter(
                listing=base, date__gte=start_day,
                date__lt=start_day + datetime.timedelta(days=days),
            )
        ],
    }


def public_schedule(listing):
    """What customers see on the venue page: the weekly hours (None = the
    default 6 AM – midnight), closures from today on, and the venue's
    booking instructions."""
    settings = settings_for(listing)
    if settings is None:
        return None
    today = today_ist().isoformat()
    from .timings import listing_timings, timings_label
    timings = listing_timings(listing)
    return {
        'hours': settings.hours,
        'timingsLabel': timings_label(timings),
        'closures': [
            {'from': c['from'], 'to': c['to'], 'reason': c.get('reason') or ''}
            for c in settings.closures or [] if c.get('to', '') >= today
        ][:10],
        'specialHours': [
            s for s in settings.special_hours or [] if s.get('date', '') >= today
        ][:10],
        'bookingInstructions': settings.booking_instructions or '',
    }
