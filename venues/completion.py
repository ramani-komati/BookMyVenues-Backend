"""
ONE source of truth for the wizard progress.

compute_completion(draft) -> (completion_percent, missing_list)

Used by every draft response AND the submit endpoint, so the frontend
progress circle always matches what submit will say.

5 buckets x 20% each (basics, location, details, payout, photos).
A bucket counts only when ALL its required items are filled.
Mirrors the frontend's own formula (contract: submit gates in 4.7).
"""


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
    if not _filled(location, 'mapsLink'):
        missing.append('Google Maps link')
    return missing


def _details(details):
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


def _payout(payout):
    return [
        label
        for key, label in REQUIRED_PAYOUT_FIELDS
        if not _filled(payout, key)
    ]


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
        _payout(data.get('payout') or {}),
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
