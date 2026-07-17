"""
Format validation for draft sections (contract 4.3).

Autosave sends partial data, so empty/absent fields are always OK —
we only reject values that are PRESENT but wrongly formatted
(e.g. a 5-digit pincode). Returns one human-readable message.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

SECTIONS = ('basics', 'location', 'details', 'payout')

# key -> (regex, error message)
_PHONE = (re.compile(r'^\d{10}$'), 'Phone number must be exactly 10 digits.')
_PINCODE = (re.compile(r'^\d{6}$'), 'Pincode must be exactly 6 digits.')
_ACCOUNT = (re.compile(r'^\d{9,18}$'), 'Account number must be 9 to 18 digits.')
_IFSC = (re.compile(r'^[A-Z]{4}0[A-Z0-9]{6}$'), 'Enter a valid IFSC code, e.g. HDFC0001234.')
_PAN = (re.compile(r'^[A-Z]{5}[0-9]{4}[A-Z]$'), 'Enter a valid PAN, e.g. ABCDE1234F.')
_UPI = (re.compile(r'^[\w.\-]{2,}@[A-Za-z]{2,}$'), 'Enter a valid UPI id, e.g. name@upi.')

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
    },
}

# Fields validated as email addresses, per section.
_EMAIL_FIELDS = {'basics': ('email',), 'location': (), 'details': (), 'payout': ()}


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

    for key in _EMAIL_FIELDS[section]:
        value = payload.get(key)
        if value in (None, ''):
            continue
        try:
            validate_email(str(value).strip())
        except ValidationError:
            return 'Enter a valid email address.'

    return None
