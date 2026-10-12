"""
THE canonical EVENT taxonomy — the counterpart to venues/taxonomy.py.

Deliberately separate from the venue taxonomy: an event is not a venue, and
putting "Navaratri" into venues.taxonomy.CATEGORIES would offer it as a
registration category in the venue wizard and leak it into /api/venues search.

Served by GET /api/config under `eventCategories` so the customer app, the
vendor portal and the admin panel all read one list instead of hard-coding
their own copies.
"""

# Display order is the order customers see in the Events filter row.
EVENT_CATEGORIES = [
    'Navaratri',
    'Dandiya',
    'Garba Night',
    'Concert',
    'DJ Night',
    'Comedy Show',
    'New Year Party',
    'Festival',
    'Workshop',
    'Sports Event',
    'Other',
]

_ALIASES = {
    'navratri': 'Navaratri',
    'navarathri': 'Navaratri',
    'navaratri': 'Navaratri',
    'dandia': 'Dandiya',
    'dandiya': 'Dandiya',
    'dandiya night': 'Dandiya',
    'garba': 'Garba Night',
    'garba night': 'Garba Night',
    'dj': 'DJ Night',
    'dj night': 'DJ Night',
    'concert': 'Concert',
    'comedy': 'Comedy Show',
    'comedy show': 'Comedy Show',
    'new year': 'New Year Party',
    'new year party': 'New Year Party',
    'nye': 'New Year Party',
}

MAX_CATEGORY_LENGTH = 50  # matches the indexed Event.category column


def canonical_event_category(raw):
    """Map a stored/typed event category onto its canonical spelling.

    Unknown values pass through trimmed rather than being dropped — the same
    forgiving contract venues.taxonomy.canonical_category offers, so a vendor
    typing something new never loses their data.
    """
    text = str(raw or '').strip()
    if not text:
        return ''
    if text in EVENT_CATEGORIES:
        return text
    return _ALIASES.get(text.lower(), text)[:MAX_CATEGORY_LENGTH]


def event_categories_payload():
    """The list GET /api/config serves under `eventCategories`."""
    return list(EVENT_CATEGORIES)
