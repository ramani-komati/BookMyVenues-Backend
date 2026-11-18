"""
Format validation for draft sections (contract 4.3).

Autosave sends partial data, so empty/absent fields are always OK —
we only reject values that are PRESENT but wrongly formatted
(e.g. a 5-digit pincode). Returns one human-readable message.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .geo import valid_coordinates

SECTIONS = ('basics', 'location', 'details', 'payout')

# key -> (regex, error message)
_PHONE = (re.compile(r'^\d{10}$'), 'Phone number must be exactly 10 digits.')
# Indian PIN codes never start with 0 (the first digit is the postal zone).
# Whether the zone matches the chosen state is a SUBMIT check
# (venues/regions.py), because autosave may arrive before the state does.
_PINCODE = (re.compile(r'^[1-9]\d{5}$'), 'Pincode must be 6 digits and cannot start with 0.')
_ACCOUNT = (re.compile(r'^\d{9,18}$'), 'Account number must be 9 to 18 digits.')
_IFSC = (re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$'), 'Enter a valid IFSC code, e.g. HDFC0001234.')
_PAN = (re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$'), 'Enter a valid PAN, e.g. ABCDE1234F.')
_UPI = (re.compile(r'^[\w.\-]{2,}@[A-Za-z]{2,}$'), 'Enter a valid UPI id, e.g. name@upi.')
_GSTIN = (
    re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'),
    'Enter a valid 15-character GSTIN, e.g. 36ABCDE1234F1Z5.',
)

# Which formatted fields each section may contain.
_SECTION_RULES = {
    'basics': {'phone': _PHONE},
    'location': {'pincode': _PINCODE},
    'details': {},
    'payout': {
        'phone': _PHONE,
        'accountNumber': _ACCOUNT,
        'ifsc': _IFSC,
        'pan': _PAN,
        'upi': _UPI,
        'upiId': _UPI,
        # Optional business documents (Resort / Big Screen Surprise forms).
        'gstin': _GSTIN,
    },
}

# Fields validated as email addresses, per section.
_EMAIL_FIELDS = {'basics': ('email',), 'location': (), 'details': (), 'payout': ()}


# India's extent (with a margin) — a pin outside it is a typo, not a venue.
INDIA_LAT = (6.0, 38.0)
INDIA_LNG = (68.0, 98.0)


def _map_pin_error(location):
    """The optional map-location pin (latitude + longitude, set from the
    vendor's GPS, their address or typed in). Both or neither."""
    lat, lng = location.get('latitude'), location.get('longitude')
    if lat in (None, '') and lng in (None, ''):
        return None
    point = valid_coordinates(lat, lng) if lat not in (None, '') and lng not in (None, '') else None
    if point is None:
        return 'Map location needs a valid latitude and longitude.'
    if not (INDIA_LAT[0] <= point[0] <= INDIA_LAT[1] and INDIA_LNG[0] <= point[1] <= INDIA_LNG[1]):
        return 'Map location must be inside India.'
    return None


def validate_section(section, payload):
    """Returns an error message string, or None when the payload is fine."""
    if section not in SECTIONS:
        return f'Unknown section "{section}". Allowed: {", ".join(SECTIONS)}.'
    if not isinstance(payload, dict):
        return 'Section data must be a JSON object.'

    for key, (pattern, message) in _SECTION_RULES[section].items():
        value = payload.get(key)
        if value in (None, ''):
            continue  # empty is fine — drafts are allowed to be partial
        if not pattern.fullmatch(str(value).strip()):
            return message

    if section == 'location':
        error = _map_pin_error(payload)
        if error:
            return error

    for key in _EMAIL_FIELDS[section]:
        value = payload.get(key)
        if value in (None, ''):
            continue
        try:
            validate_email(str(value).strip())
        except ValidationError:
            return 'Enter a valid email address.'

    return None
