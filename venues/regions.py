"""
India's states, union territories and districts — ONE dataset for the whole
platform (venues/data/india_regions.json).

    states()                      every state / UT, in name order
    find_state('TS')              -> the 'Telangana' entry (names, ISO codes, aliases)
    find_district(state, 'Medchal–Malkajgiri') -> 'Medchal Malkajgiri'
    pincode_error(pincode, state) -> None | human-readable reason
    location_problems(location)   -> submit-gate gaps for a draft's location

WHERE THE DATA COMES FROM

All 28 states and 8 union territories with their districts, from the National
Portal of India directory (see the file's `source`). Each state also carries
its ISO 3166-2 code, the first digit(s) of its PIN codes, and older or
alternative spellings ('Orissa', 'Rangareddy', 'Bangalore Urban') so values
typed by vendors, saved in older drafts or returned by Google still resolve
to ONE canonical name.

Cities, towns and localities are NOT listed here — there are tens of
thousands and they change constantly. They come from the geocoder
(venues/geocoding.py), scoped to the chosen district.

TO UPDATE when districts change: edit the JSON (add the new district, and
move a renamed district's old name into `districtAliases`), then regenerate
the frontends' copy with `manage.py india_regions_js <path>` for each app.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / 'data' / 'india_regions.json'

_PINCODE = re.compile(r'^[1-9]\d{5}$')


def _key(text):
    """Comparison form of a place name: case, punctuation, '&' and a trailing
    'district' never matter ('Medchal–Malkajgiri District' == 'medchal malkajgiri')."""
    text = str(text or '').lower().replace('&', ' and ')
    text = re.sub(r'[‐-―\-.,()\'’/]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'\s+district$', '', text)


@lru_cache(maxsize=1)
def dataset():
    with DATA_FILE.open(encoding='utf-8') as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _state_index():
    index = {}
    for state in dataset()['states']:
        for name in (state['name'], state['code'], *state['aliases']):
            index.setdefault(_key(name), state)
    return index


@lru_cache(maxsize=64)
def _district_index(state_name):
    state = find_state(state_name)
    index = {}
    if state is None:
        return index
    for name in state['districts']:
        index[_key(name)] = name
    for name, aliases in state.get('districtAliases', {}).items():
        for alias in aliases:
            index.setdefault(_key(alias), name)
    return index


def states():
    return dataset()['states']


def find_state(name):
    """The state / UT entry for a name, ISO code or alias, else None."""
    return _state_index().get(_key(name)) if str(name or '').strip() else None


def canonical_state(name):
    state = find_state(name)
    return state['name'] if state else None


def find_district(state_name, name):
    """The canonical district name inside that state, else None.

    Delhi's districts are short ('South West'); geocoders call them
    'South West Delhi', so a trailing state name is also tried without it.
    """
    state = find_state(state_name)
    if state is None or not str(name or '').strip():
        return None
    index = _district_index(state['name'])
    key = _key(name)
    found = index.get(key)
    if found is None:
        suffix = ' ' + _key(state['name'])
        if key.endswith(suffix):
            found = index.get(key[: -len(suffix)].strip())
    return found


def find_district_anywhere(name):
    """(state, district) for a district name that exists in exactly ONE
    state, else None — for providers that omit the state. Names shared by
    several states ('Aurangabad', 'Bilaspur', 'Hamirpur') stay unresolved."""
    if not str(name or '').strip():
        return None
    matches = []
    for state in states():
        district = find_district(state['name'], name)
        if district:
            matches.append((state['name'], district))
    return matches[0] if len(matches) == 1 else None


def pincode_error(pincode, state_name=None):
    """None when the PIN is valid (for that state, when one is known).

    Indian PIN codes are six digits and never start with 0. The first digit
    is the postal zone (1 Delhi/North … 8 Bihar/Jharkhand; 9 is the Army
    Postal Service, never a venue), so it must belong to the venue's state.
    """
    value = str(pincode or '').strip()
    if not _PINCODE.fullmatch(value):
        return 'Pincode must be 6 digits and cannot start with 0.'
    if value[0] == '9':
        return 'Pincodes starting with 9 are Army Postal Service codes — enter the venue’s own pincode.'
    state = find_state(state_name)
    if state is not None and value[0] not in state['pinZones']:
        return f'This pincode is not in {state["name"]} — check the pincode or the state.'
    return None


def location_problems(location):
    """Submit-gate gaps for a draft's location section (venues/completion.py).

    Empty fields are reported by the caller as missing; this only reports
    values that are present but are not a real Indian state / district / PIN.
    """
    location = location if isinstance(location, dict) else {}
    problems = []
    state_name = str(location.get('stateName') or '').strip()
    district = str(location.get('district') or '').strip()
    pincode = str(location.get('pincode') or '').strip()
    state = find_state(state_name) if state_name else None
    if state_name and state is None:
        problems.append('State (choose an Indian state or union territory)')
    if state is not None and district and find_district(state['name'], district) is None:
        problems.append(f'District (choose a district in {state["name"]})')
    if pincode and pincode_error(pincode, state['name'] if state else None):
        problems.append('A valid pincode for the venue’s state')
    return problems
