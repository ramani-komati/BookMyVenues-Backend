"""
ONE source of truth for the wizard progress.

compute_completion(draft) -> (completion_percent, missing_list)

Used by every draft response AND the submit endpoint, so the frontend
progress circle always matches what submit will say.

5 buckets x 20% each (basics, location, details, payout, photos).
A bucket counts only when ALL its required items are filled.
Mirrors the frontend's own formula (contract: submit gates in 4.7).
"""
from .regions import location_problems


def _filled(bucket, key):
    """True if the bucket has a non-empty value for this key.
    Numbers count too (capacity may arrive as 120 or "120")."""
    value = bucket.get(key)
    if value is None:
        return False
    return bool(str(value).strip())


def _basics(basics):
    missing = []
    if not _filled(basics, 'venueName'):
        missing.append('Venue name')
    if not _filled(basics, 'phone'):
        missing.append('Contact phone')
    return missing


def _location(location):
    missing = []
    if not _filled(location, 'houseStreet'):
        missing.append('Street address')
    if not _filled(location, 'pincode'):
        missing.append('Pincode')
    if not _filled(location, 'stateName'):
        missing.append('State')
    if not _filled(location, 'district'):
        missing.append('District')
    if not _filled(location, 'city'):
        missing.append('City / Town')
    if not _filled(location, 'mapsLink'):
        missing.append('Google Maps link')
    # Present but not a real Indian state / district / PIN for that state
    # (venues/regions.py — every state and union territory is accepted).
    missing.extend(location_problems(location))
    return missing


def _enabled_rows(block, key, priced=('price',)):
    """Entries of block[key] that are switched on and carry a price."""
    rows = (block or {}).get(key) if isinstance(block, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or row.get('enabled') is False:
            continue
        if all(_filled(row, field) for field in priced):
            out.append(row)
    return out


def _offering_details(venue_type, details):
    """The submit gate for the three venue types that sell memberships,
    packages or surprises instead of hourly slots (offerings app)."""
    missing = []
    if venue_type == 'Gym':
        # Memberships are THE product of a gym listing — mandatory.
        if not _enabled_rows(details.get('fitness'), 'plans'):
            missing.append('At least one membership plan with a price')
    elif venue_type == 'Resort':
        # New resorts sell packages; a resort registered before packages
        # existed keeps its hourly lawn pricing as an alternative.
        legacy = _filled(details, 'capacity') and details.get('packages')
        if not _enabled_rows(details.get('resort'), 'packages') and not legacy:
            missing.append('At least one resort package with a price')
    elif venue_type == 'Big Screen Surprise':
        surprise = details.get('surprise') if isinstance(details.get('surprise'), dict) else {}
        if not _filled(surprise, 'price'):
            missing.append('Price per surprise')
        if not _enabled_rows(surprise, 'shows', priced=('movie',)):
            missing.append('At least one show (movie, screen and timings)')
    return missing


OFFERING_VENUE_TYPES = ('Gym', 'Resort', 'Big Screen Surprise')


def _details(details):
    venue_type = str(details.get('primaryCategory') or '').strip()
    if venue_type in OFFERING_VENUE_TYPES:
        return _offering_details(venue_type, details)

    missing = []
    if not _filled(details, 'primaryCategory'):
        missing.append('Venue category')
    if not _filled(details, 'capacity'):
        missing.append('Capacity')
    if not details.get('amenities'):
        missing.append('At least one amenity')

    # Pricing rule depends on category: Playzone -> sports, others -> packages.
    category = str(details.get('primaryCategory') or '').strip().lower()
    if 'playzone' in category:
        if not details.get('sports'):
            missing.append('At least one sport with pricing')
    elif not details.get('packages'):
        missing.append('At least one package')
    return missing


# The 5 bank fields the payout form requires.
REQUIRED_PAYOUT_FIELDS = [
    ('accountHolder', 'Account holder name'),
    ('bankName', 'Bank name'),
    ('accountNumber', 'Account number'),
    ('ifsc', 'IFSC code'),
    ('phone', 'Payout phone'),
]


def _payout(payout, venue_type=''):
    missing = [
        label
        for key, label in REQUIRED_PAYOUT_FIELDS
        if not _filled(payout, key)
    ]
    # The Fitness / Resort / Big Screen Surprise forms ask the owner to
    # accept the platform commission and terms with their bank details.
    if venue_type in OFFERING_VENUE_TYPES and payout.get('agreedToTerms') is not True:
        missing.append('Agreement to the platform commission and terms')
    return missing


def _photos(photos):
    if not photos.get('venuePhotos'):
        return ['At least one venue photo']
    return []


def compute_completion(draft):
    """Returns (percent 0-100 in steps of 20, list of human-readable gaps)."""
    data = draft.data or {}
    checks = [
        _basics(data.get('basics') or {}),
        _location(data.get('location') or {}),
        _details(data.get('details') or {}),
        _payout(
            data.get('payout') or {},
            str((data.get('details') or {}).get('primaryCategory') or '').strip(),
        ),
        _photos(data.get('photos') or {}),
    ]

    missing = []
    completed = 0
    for bucket_missing in checks:
        if bucket_missing:
            missing.extend(bucket_missing)
        else:
            completed += 1
    return completed * 20, missing
