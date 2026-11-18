"""
Validation of the `detail.fitness` / `detail.resort` / `detail.surprise`
blocks a venue record carries.

clean_record(record) is called on EVERY publish (vendor, super admin
registration — same endpoint — and super admin edit). It returns the record
with its block normalised (numbers as ints, times as "HH:MM", unknown keys
dropped) or raises OfferingError naming the first problem.

It checks FORMAT, not completeness: the vendor portal republishes a venue
from its draft on every dashboard visit ("self-heal"), so refusing a
half-configured block here would lock a vendor out of their own dashboard.
Completeness ("at least one membership plan") is the draft SUBMIT gate's job
(venues/completion.py) and the forms'.
"""
import datetime
import re

from . import catalog

_TIME = re.compile(r'^([01]\d|2[0-3]):([0-5]\d)$')
_KEY = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


class OfferingError(ValueError):
    pass


# --- primitives ---------------------------------------------------------------

def _text(value, label, limit, required=False):
    text = '' if value is None else str(value).strip()
    if required and not text:
        raise OfferingError(f'{label} is required.')
    if len(text) > limit:
        raise OfferingError(f'{label} must be {limit} characters or fewer.')
    return text


def _bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('true', '1', 'yes', 'on')


def _int(value, label, *, minimum=0, maximum=catalog.MAX_PRICE, required=False):
    """Whole number from JSON ('1499', 1499, '1,499'); None when blank."""
    if value is None or str(value).strip() == '':
        if required:
            raise OfferingError(f'{label} is required.')
        return None
    text = str(value).strip().replace(',', '')
    try:
        number = float(text)
    except ValueError:
        raise OfferingError(f'{label} must be a number.') from None
    if number != int(number):
        raise OfferingError(f'{label} must be a whole number.')
    number = int(number)
    if number < minimum:
        raise OfferingError(f'{label} must be at least {minimum}.')
    if number > maximum:
        raise OfferingError(f'{label} must be at most {maximum:,}.')
    return number


def _time(value, label, required=False):
    text = '' if value is None else str(value).strip()
    if not text:
        if required:
            raise OfferingError(f'{label} is required.')
        return ''
    if not _TIME.match(text):
        raise OfferingError(f'{label} must be a time like 09:30.')
    return text


def minutes(text):
    hours, mins = text.split(':')
    return int(hours) * 60 + int(mins)


def _date(value, label):
    text = '' if value is None else str(value).strip()
    if not text:
        return ''
    try:
        datetime.date.fromisoformat(text)
    except ValueError:
        raise OfferingError(f'{label} must be a date (YYYY-MM-DD).') from None
    return text


def _date_window(entry, label):
    start = _date(entry.get('availableFrom', entry.get('from')), f'{label}: available from')
    end = _date(entry.get('availableTo', entry.get('to')), f'{label}: available to')
    if start and end and end < start:
        raise OfferingError(f'{label}: the end date is before the start date.')
    return start, end


def _choices(values, allowed, label, *, other_allowed=True):
    """A multi-select list: known options in their canonical spelling plus,
    when allowed, vendor-typed extras. De-duplicated, order kept."""
    if values is None:
        return []
    if isinstance(values, dict):          # {"Yoga": true} map shape
        values = [k for k, on in values.items() if on]
    if not isinstance(values, (list, tuple)):
        raise OfferingError(f'{label} must be a list.')
    canonical = {str(a).lower(): a for a in allowed}
    out = []
    for raw in values:
        text = str(raw or '').strip()
        if not text:
            continue
        value = canonical.get(text.lower())
        if value is None:
            if not other_allowed:
                raise OfferingError(f'{label}: "{text}" is not an option.')
            value = _text(text, label, 60)
        if value not in out:
            out.append(value)
    if len(out) > catalog.MAX_LIST_ITEMS:
        raise OfferingError(f'{label}: too many entries.')
    return out


def _days(values, label):
    if values in (None, ''):
        return []
    if isinstance(values, dict):
        values = [k for k, on in values.items() if on]
    if not isinstance(values, (list, tuple)):
        raise OfferingError(f'{label} must be a list of weekdays.')
    picked = {str(v).strip().lower()[:3] for v in values if str(v).strip()}
    unknown = picked - set(catalog.WEEKDAYS)
    if unknown:
        raise OfferingError(f'{label}: unknown day "{sorted(unknown)[0]}".')
    return [d for d in catalog.WEEKDAYS if d in picked]


def _list(value, label, limit):
    if value in (None, ''):
        return []
    if not isinstance(value, (list, tuple)):
        raise OfferingError(f'{label} must be a list.')
    if len(value) > limit:
        raise OfferingError(f'{label}: at most {limit} allowed.')
    return [v for v in value if isinstance(v, dict)]


def _closures(value, label='Closures'):
    out = []
    for entry in _list(value, label, catalog.MAX_CLOSURES):
        start = _date(entry.get('from'), f'{label}: from')
        end = _date(entry.get('to') or entry.get('from'), f'{label}: to')
        if not start:
            continue                        # an empty row the form left
        if end < start:
            raise OfferingError(f'{label}: a closure ends before it starts.')
        out.append({'from': start, 'to': end, 'reason': _text(entry.get('reason'), f'{label}: reason', 120)})
    return out


def _ranges(value, label, limit=catalog.MAX_SLOTS, required=False):
    """[{start, end}] same-day time ranges, sorted, non-overlapping."""
    out = []
    for entry in _list(value, label, limit):
        start = _time(entry.get('start'), f'{label}: start')
        end = _time(entry.get('end'), f'{label}: end')
        if not start and not end:
            continue
        if not start or not end:
            raise OfferingError(f'{label}: give both a start and an end time.')
        if minutes(end) <= minutes(start):
            raise OfferingError(f'{label}: {start}–{end} must end after it starts.')
        out.append({'start': start, 'end': end})
    if required and not out:
        raise OfferingError(f'{label}: add at least one time slot.')
    out.sort(key=lambda r: r['start'])
    for before, after in zip(out, out[1:]):
        if minutes(after['start']) < minutes(before['end']):
            raise OfferingError(f'{label}: {before["start"]}–{before["end"]} overlaps {after["start"]}–{after["end"]}.')
    return out


def _keyed(entries, label):
    """Each entry's stable id; missing or repeated ids are refused (the form
    always sends one — an id is what keeps a customer's membership attached
    to the plan they bought)."""
    seen = set()
    for index, entry in enumerate(entries, start=1):
        key = str(entry.get('id') or '').strip()
        if not _KEY.match(key):
            raise OfferingError(f'{label} #{index} has no valid id — reload the form and save again.')
        if key in seen:
            raise OfferingError(f'{label} #{index} repeats another entry’s id.')
        seen.add(key)
        yield index, key, entry


# --- fitness ------------------------------------------------------------------

def clean_fitness(block, detail):
    block = block if isinstance(block, dict) else {}
    plans = []
    for index, key, raw in _keyed(_list(block.get('plans'), 'Membership plans', catalog.MAX_PLANS), 'Membership plan'):
        kind = str(raw.get('kind') or 'custom').strip()
        if kind not in catalog.PLAN_KINDS:
            raise OfferingError(f'Membership plan #{index}: unknown plan type.')
        label = f'Membership plan “{str(raw.get("name") or catalog.PLAN_KINDS[kind]["name"]).strip()[:40]}”'
        enabled = _bool(raw.get('enabled', True))
        unit = str(raw.get('durationUnit') or 'month').strip()
        if unit not in catalog.DURATION_UNITS:
            raise OfferingError(f'{label}: duration must be in days, weeks or months.')
        start_rule = str(raw.get('startRule') or 'purchase').strip()
        if start_rule not in catalog.START_RULES:
            raise OfferingError(f'{label}: unknown start rule.')
        expiry_rule = str(raw.get('expiryRule') or 'duration').strip()
        if expiry_rule not in catalog.EXPIRY_RULES:
            raise OfferingError(f'{label}: unknown expiry rule.')
        visits = _int(raw.get('visits'), f'{label}: permitted visits / classes', minimum=1, maximum=10000)
        if expiry_rule == 'visits_or_duration' and visits is None:
            raise OfferingError(f'{label}: “ends when visits run out” needs a number of visits.')
        trial = _bool(raw.get('trialAvailable'))
        plans.append({
            'id': key,
            'kind': kind,
            'name': _text(raw.get('name') or catalog.PLAN_KINDS[kind]['name'], f'{label}: name', 120, required=True),
            'description': _text(raw.get('description'), f'{label}: description', 500),
            'enabled': enabled,
            # An enabled plan must have a real price; a disabled one may be
            # left half-filled while the vendor sets it up.
            'price': _int(raw.get('price'), f'{label}: price', minimum=1, required=enabled),
            'durationValue': _int(raw.get('durationValue'), f'{label}: duration', minimum=1, maximum=1000, required=True),
            'durationUnit': unit,
            'startRule': start_rule,
            'startWithinDays': (
                _int(raw.get('startWithinDays'), f'{label}: start within (days)', minimum=1,
                     maximum=catalog.MAX_START_WITHIN_DAYS) or 30
            ) if start_rule == 'chosen' else 0,
            'expiryRule': expiry_rule,
            'visits': visits,
            'facilities': _choices(raw.get('facilities'), catalog.FITNESS_AMENITIES + catalog.FITNESS_SERVICES, f'{label}: included facilities'),
            'trialAvailable': trial,
            'trialPrice': _int(raw.get('trialPrice'), f'{label}: trial price') if trial else None,
            'joiningFee': _int(raw.get('joiningFee'), f'{label}: joining fee') or 0,
        })

    class_timings = []
    for entry in _list(block.get('classTimings'), 'Class timings', catalog.MAX_SLOTS):
        name = _text(entry.get('name'), 'Class timings: class name', 60)
        start = _time(entry.get('start'), f'Class “{name}”: start')
        end = _time(entry.get('end'), f'Class “{name}”: end')
        if not name and not start and not end:
            continue
        if not (name and start and end):
            raise OfferingError('Class timings: give each class a name, a start and an end time.')
        if minutes(end) <= minutes(start):
            raise OfferingError(f'Class “{name}” must end after it starts.')
        class_timings.append({'name': name, 'days': _days(entry.get('days'), f'Class “{name}”: days'), 'start': start, 'end': end})

    return {
        'services': _choices(block.get('services'), catalog.FITNESS_SERVICES, 'Fitness services'),
        'operatingDays': _days(block.get('operatingDays'), 'Days of operation'),
        'classTimings': class_timings,
        'maxMembersAtOnce': _int(block.get('maxMembersAtOnce'), 'Maximum members at one time', minimum=1, maximum=100000),
        'maxClassCapacity': _int(block.get('maxClassCapacity'), 'Maximum capacity per group class', minimum=1, maximum=10000),
        'peakHours': _ranges(block.get('peakHours'), 'Peak hours'),
        'closures': _closures(block.get('closures'), 'Holiday / temporary closures'),
        'plans': plans,
    }


# --- resort -------------------------------------------------------------------

def clean_resort(block, detail):
    block = block if isinstance(block, dict) else {}
    check_in = _time(block.get('checkIn'), 'Check-in time')
    check_out = _time(block.get('checkOut'), 'Check-out time')

    facilities = block.get('facilities') if isinstance(block.get('facilities'), dict) else {}
    accommodation = block.get('accommodation') if isinstance(block.get('accommodation'), dict) else {}
    has_rooms = _bool(accommodation.get('available'))
    rooms = []
    if has_rooms:
        for entry in _list(accommodation.get('rooms'), 'Accommodation', 20):
            room_type = _text(entry.get('type'), 'Accommodation: room type', 60)
            if not room_type:
                continue
            rooms.append({
                'type': room_type,
                'count': _int(entry.get('count'), f'Room “{room_type}”: number of rooms', minimum=1, maximum=1000),
                'maxGuests': _int(entry.get('maxGuests'), f'Room “{room_type}”: guests per room', minimum=1, maximum=50),
                'pricePerNight': _int(entry.get('pricePerNight'), f'Room “{room_type}”: price per night'),
            })

    packages = []
    for index, key, raw in _keyed(_list(block.get('packages'), 'Resort packages', catalog.MAX_PACKAGES), 'Resort package'):
        name = _text(raw.get('name'), f'Resort package #{index}: name', 120, required=True)
        label = f'Package “{name[:40]}”'
        kind = str(raw.get('kind') or 'day').strip()
        if kind not in catalog.PACKAGE_KINDS:
            raise OfferingError(f'{label}: unknown package type.')
        enabled = _bool(raw.get('enabled', True))
        pricing = str(raw.get('pricing') or ('per_person')).strip()
        if pricing not in catalog.PACKAGE_PRICING:
            raise OfferingError(f'{label}: unknown pricing.')
        if kind == 'pool' and pricing == 'per_package':
            raise OfferingError(f'{label}: pool entry is priced per person or per hour.')
        duration = ''
        slots = []
        pkg_in = pkg_out = ''
        if kind == 'day':
            duration = str(raw.get('duration') or 'full_day').strip()
            if duration not in catalog.PACKAGE_DURATIONS:
                raise OfferingError(f'{label}: choose Half Day or Full Day.')
            if pricing == 'per_hour':
                raise OfferingError(f'{label}: per-hour pricing is for pool / water-park entry.')
            if duration == 'half_day':
                slots = _ranges(raw.get('slots'), f'{label}: half-day slots', required=enabled)
            else:
                pkg_in = _time(raw.get('checkIn'), f'{label}: check-in time') or check_in
                pkg_out = _time(raw.get('checkOut'), f'{label}: check-out time') or check_out
                if enabled and not (pkg_in and pkg_out):
                    raise OfferingError(f'{label}: set the check-in and check-out times (or the resort’s own).')
                if pkg_in and pkg_out and minutes(pkg_out) <= minutes(pkg_in):
                    raise OfferingError(f'{label}: check-out must be after check-in on the same day.')
        else:
            slots = _ranges(raw.get('slots'), f'{label}: operating time slots', required=enabled)

        min_persons = _int(raw.get('minPersons'), f'{label}: minimum persons', minimum=1, maximum=catalog.MAX_PERSONS) or 1
        max_persons = _int(raw.get('maxPersons'), f'{label}: maximum persons', minimum=1, maximum=catalog.MAX_PERSONS, required=enabled) or min_persons
        if max_persons < min_persons:
            raise OfferingError(f'{label}: maximum persons is below the minimum.')
        capacity = _int(raw.get('capacity'), f'{label}: guests per slot (all bookings)', minimum=1, maximum=100000)
        if capacity is not None and capacity < min_persons:
            raise OfferingError(f'{label}: guests per slot is below the minimum persons per booking.')
        start, end = _date_window(raw, label)
        inclusions = _choices(raw.get('inclusions'), catalog.RESORT_INCLUSIONS, f'{label}: what’s included')
        other = _text(raw.get('otherInclusion'), f'{label}: other inclusion', 120)
        packages.append({
            'id': key,
            'name': name,
            'description': _text(raw.get('description'), f'{label}: description', 500),
            'kind': kind,
            'duration': duration,
            'pricing': pricing,
            'price': _int(raw.get('price'), f'{label}: {"adult " if kind == "pool" else ""}price', minimum=1, required=enabled),
            'childPrice': _int(raw.get('childPrice'), f'{label}: child price'),
            'minPersons': min_persons,
            'maxPersons': max_persons,
            'capacity': capacity,
            'slots': slots,
            'checkIn': pkg_in,
            'checkOut': pkg_out,
            'availableFrom': start,
            'availableTo': end,
            'inclusions': inclusions,
            'otherInclusion': other,
            'enabled': enabled,
        })

    return {
        'types': _choices(block.get('types'), catalog.RESORT_TYPES, 'Resort type', other_allowed=False),
        'otherType': _text(block.get('otherType'), 'Other resort type', 60),
        'yearEstablished': _int(block.get('yearEstablished'), 'Year established', minimum=1800,
                                maximum=datetime.date.today().year),
        'facilities': {
            group: _choices(facilities.get(group), options, 'Facilities')
            for group, options in catalog.RESORT_FACILITIES.items()
        },
        'accommodation': {
            'available': has_rooms,
            'maxOvernight': _int(accommodation.get('maxOvernight'), 'Maximum overnight capacity', minimum=1, maximum=100000) if has_rooms else None,
            'rooms': rooms,
            'details': _text(accommodation.get('details'), 'Accommodation details', 500) if has_rooms else '',
        },
        'checkIn': check_in,
        'checkOut': check_out,
        'availableDays': _days(block.get('availableDays'), 'Available days'),
        'closures': _closures(block.get('closures'), 'Unavailable dates'),
        'packages': packages,
    }


# --- big screen surprise ------------------------------------------------------

def _url_list(value, label, limit=10):
    out = []
    for item in value or []:
        url = str((item.get('url') if isinstance(item, dict) else item) or '').strip()
        if not url:
            continue
        if not url.startswith('https://') or len(url) > 500:
            raise OfferingError(f'{label}: photos must be uploaded through the form.')
        out.append(url)
    return out[:limit]


def clean_surprise(block, detail):
    block = block if isinstance(block, dict) else {}
    screens = _int(block.get('screens'), 'Number of screens for this service', minimum=1, maximum=catalog.MAX_SCREENS) or 1
    occasions = _choices(detail.get('occasions') if block.get('occasions') is None else block.get('occasions'),
                         catalog.SURPRISE_OCCASIONS, 'Occasions')
    prices = block.get('occasionPrices') if isinstance(block.get('occasionPrices'), dict) else {}
    occasion_prices = {}
    for occasion, value in prices.items():
        amount = _int(value, f'Price for {occasion}', minimum=1)
        if amount is not None and occasion in occasions:
            occasion_prices[occasion] = amount

    shows = []
    for index, key, raw in _keyed(_list(block.get('shows'), 'Shows', catalog.MAX_SHOWS), 'Show'):
        movie = _text(raw.get('movie'), f'Show #{index}: movie', 150, required=True)
        label = f'Show “{movie[:40]}”'
        screen = _int(raw.get('screen'), f'{label}: screen', minimum=1, maximum=catalog.MAX_SCREENS) or 1
        if screen > screens:
            raise OfferingError(f'{label}: screen {screen} — the theatre offers {screens} screen{"s" if screens != 1 else ""} for this service.')
        times = []
        for value in (raw.get('times') or [])[:catalog.MAX_TIMES_PER_SHOW + 1]:
            text = _time(value, f'{label}: show time')
            if text and text not in times:
                times.append(text)
        if len(times) > catalog.MAX_TIMES_PER_SHOW:
            raise OfferingError(f'{label}: at most {catalog.MAX_TIMES_PER_SHOW} show times.')
        enabled = _bool(raw.get('enabled', True))
        if enabled and not times:
            raise OfferingError(f'{label}: add at least one show time.')
        start, end = _date_window(raw, label)
        shows.append({
            'id': key, 'movie': movie,
            'language': _text(raw.get('language'), f'{label}: language', 40),
            'screen': screen, 'times': sorted(times),
            'availableFrom': start, 'availableTo': end, 'enabled': enabled,
        })

    # One screen plays one show at a time: two shows on the same screen at the
    # same time with overlapping dates would sell that one slot twice.
    def overlap(a, b):
        return (not a['availableTo'] or not b['availableFrom'] or b['availableFrom'] <= a['availableTo']) and \
               (not b['availableTo'] or not a['availableFrom'] or a['availableFrom'] <= b['availableTo'])
    active = [s for s in shows if s['enabled']]
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if a['screen'] == b['screen'] and set(a['times']) & set(b['times']) and overlap(a, b):
                clash = sorted(set(a['times']) & set(b['times']))[0]
                raise OfferingError(
                    f'Screen {a["screen"]} has two shows at {clash} (“{a["movie"][:30]}” and “{b["movie"][:30]}”). '
                    'Give them different times, screens or dates.'
                )

    start, end = _date_window(block, 'Surprise availability')
    return {
        'brand': _text(block.get('brand'), 'Cinema brand', 40),
        'brandOther': _text(block.get('brandOther'), 'Cinema brand', 60),
        'theatreType': _text(block.get('theatreType'), 'Theatre type', 20),
        'formats': _choices(block.get('formats'), catalog.SCREEN_FORMATS, 'Screen format', other_allowed=False),
        'formatOther': _text(block.get('formatOther'), 'Other screen format', 40),
        'screens': screens,
        'showsPerDay': _int(block.get('showsPerDay'), 'Surprise shows per day', minimum=1, maximum=1000),
        'experience': _text(block.get('experience'), 'Short description of the experience', 500),
        'occasions': occasions,
        'occasionOther': _text(block.get('occasionOther'), 'Other occasion', 60),
        'price': _int(block.get('price'), 'Price per surprise', minimum=1),
        'occasionPrices': occasion_prices,
        'maxBookingsPerShow': _int(block.get('maxBookingsPerShow'), 'Maximum bookings per show', minimum=1, maximum=50) or 1,
        'minLeadHours': _int(block.get('minLeadHours'), 'Book at least (hours before the show)', minimum=0, maximum=168),
        'availableFrom': start,
        'availableTo': end,
        'availableDays': _days(block.get('availableDays'), 'Available days'),
        'closures': _closures(block.get('closures'), 'Unavailable dates'),
        'allowPhoto': _bool(block.get('allowPhoto')),
        'allowVideo': _bool(block.get('allowVideo')),
        'logo': (_url_list([block.get('logo')], 'Theatre logo', 1) or [''])[0],
        'exteriorImages': _url_list(block.get('exteriorImages'), 'Theatre exterior images'),
        'auditoriumImages': _url_list(block.get('auditoriumImages'), 'Auditorium images'),
        'screenImages': _url_list(block.get('screenImages'), 'Screen images'),
        'shows': shows,
    }


CLEANERS = {'fitness': clean_fitness, 'resort': clean_resort, 'surprise': clean_surprise}


def offering_kind(record):
    """'fitness' | 'resort' | 'surprise' when the record's venue type sells
    one, else ''. Read from the registered venue type (detail.primaryCategory)
    — the catalogue category alone can't tell a gym typed under "Others"."""
    detail = (record or {}).get('detail') or {}
    if not isinstance(detail, dict):
        return ''
    venue_type = str(detail.get('primaryCategory') or '').strip()
    if venue_type in catalog.OFFERING_TYPES:
        return catalog.OFFERING_TYPES[venue_type]
    from venues.taxonomy import canonical_category
    category = canonical_category((record or {}).get('category'))
    return {'Gym': 'fitness', 'Resort': 'resort', 'Big screen surprise': 'surprise'}.get(category, '')


def clean_record(record):
    """The record with its offering block validated + normalised, unchanged
    when its venue type sells none or it carries no block (a legacy resort
    with only hourly lawns keeps working exactly as before)."""
    kind = offering_kind(record)
    detail = (record or {}).get('detail')
    if not kind or not isinstance(detail, dict) or kind not in detail:
        return record
    cleaned = CLEANERS[kind](detail.get(kind), detail)
    return {**record, 'detail': {**detail, kind: cleaned}}
