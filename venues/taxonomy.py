"""
THE canonical venue taxonomy — one source of truth for registration, the
customer catalogue, search filters and the admin panel.

It is served by GET /api/config so every client reads the same list instead
of hard-coding its own copy; that is what stops the categories drifting apart
again (registration said "Private Hall" while the catalogue said "Party hall",
and "Box cricket" was stored as a top-level category when it is really a Play
zone sub-category).

Legacy values already in the database are ALIASED rather than migrated, so old
venues keep working and no data rewrite is needed.
"""

CATEGORIES = {
    'Party hall': [
        'Birthday Party', 'Anniversary Celebration', 'Bride to Be',
        'Baby Shower', 'Marriage Proposal', 'Engagement', 'Naming Ceremony',
        'Seminar & Meetings', 'Kitty Party', 'Family Gatherings',
        'Corporate Events', 'Farewell Party', 'Bachelor Party',
    ],
    'Private theatre': [
        'Movie Screening', 'OTT Movies', 'Live Sports Screening',
        'Live Streaming', 'Birthday Celebration', 'Anniversary Celebration',
        'Marriage Proposal', 'Surprise Celebration',
        'Friends & Family Movie Night',
    ],
    'Play zone': [
        'Box Cricket', 'Badminton', 'Pickleball', 'Football Turf',
        'Basketball', 'Volleyball', 'Swimming Pool', 'Table Tennis',
        'Skating', 'Gaming Zone', 'PS5',
    ],
    'Resort': [
        'Day Outing', 'Night Outing', 'Family Gateway', 'Couple stay',
        'Pool Party', 'Wedding Events', 'Campfire', 'Weekend Stay',
        'Get Together Party',
    ],
    'Playstation': [
        'PS5', 'PS4', 'VR Gaming', 'Racing Simulator', 'Multiplayer Gaming',
        'Tournament Play', 'Birthday Celebration', 'Friends Hangout',
    ],
    # Proposed — product had no list for this one yet.
    'Open-air theatre': [
        'Movie Screening', 'Live Sports Screening', 'Birthday Celebration',
        'Anniversary Celebration', 'Marriage Proposal', 'Surprise Celebration',
        'Rooftop Party', 'Private Dining',
    ],
    # Fitness — the services a gym offers (the Fitness registration form's
    # list, offerings/catalog.FITNESS_SERVICES). Gyms used to have no
    # category at all (they could only register under "Others"); the
    # earlier short names ('Cardio', 'Group Classes'…) still map onto these
    # through canonical_sub_category.
    'Gym': [
        'General Gym / Strength Training', 'Cardio Training', 'Personal Training',
        'Weight Loss Programs', 'Yoga', 'Zumba', 'CrossFit / Functional Training',
        'Pilates', 'Group Fitness Classes', 'Martial Arts / Boxing',
        'Dance Fitness', 'Physiotherapy / Recovery',
    ],
    # Celebrate → Big Screen Surprise: a personal message played on a real
    # cinema screen. Its sub-categories are the occasions it is booked for.
    'Big screen surprise': [
        'Birthday', 'Anniversary', 'Proposal', 'Special Wishes', 'Love',
        'Friendship', 'Selfie on Screen', 'Other',
    ],
}

# THE primary categories customers browse and vendors register under. Each
# groups one or more stored venue types (CATEGORIES keys) — the stored value
# is never renamed, so every existing venue and filter keeps working:
#   Celebrate  Party hall (the wizard's "Private Hall"), Private theatre,
#              Open-air theatre, Big screen surprise
#   Sports     Play zone (formerly the "Playzone" category)
#   Gaming     Playstation (formerly the "PlayStation" category)
#   Resorts    Resort
#   Fitness    Gym
# "Explore" is the customer home page and "Events" the ticketing product —
# neither is a venue category, so neither is here (events have their own
# taxonomy in events/catalog.py).
CATEGORY_GROUPS = [
    {'key': 'celebrate', 'label': 'Celebrate',
     'types': ['Party hall', 'Private theatre', 'Open-air theatre', 'Big screen surprise']},
    {'key': 'sports', 'label': 'Sports', 'types': ['Play zone']},
    {'key': 'gaming', 'label': 'Gaming', 'types': ['Playstation']},
    {'key': 'resorts', 'label': 'Resorts', 'types': ['Resort']},
    {'key': 'fitness', 'label': 'Fitness', 'types': ['Gym']},
]
_GROUP_OF = {t.lower(): g['key'] for g in CATEGORY_GROUPS for t in g['types']}

# The registration wizards' stored venue-type values (draft
# details.primaryCategory) — kept as they always were so every existing
# draft still opens — and the catalogue category each publishes as.
OTHERS = 'Others'
VENUE_TYPES = {
    'Private Hall': 'Party hall',
    'Private Theatre': 'Private theatre',
    'Open Theatre': 'Open-air theatre',
    'Playzone': 'Play zone',
    'Playstation': 'Playstation',
    'Resort': 'Resort',
    'Gym': 'Gym',
    'Big Screen Surprise': 'Big screen surprise',
    OTHERS: None,       # the vendor types the category name
}

# Everything the database has ever stored -> the canonical category.
_CATEGORY_ALIASES = {
    'party hall': 'Party hall',
    'private hall': 'Party hall',
    'hall': 'Party hall',
    'banquet hall': 'Party hall',
    'private theatre': 'Private theatre',
    'private theater': 'Private theatre',
    'theatre': 'Private theatre',
    'play zone': 'Play zone',
    'playzone': 'Play zone',
    'box cricket': 'Play zone',      # was mis-filed as a top-level category
    'turf': 'Play zone',
    'sports': 'Play zone',
    'resort': 'Resort',
    'playstation': 'Playstation',
    'play station': 'Playstation',
    'ps5 lounge': 'Playstation',
    'gaming lounge': 'Playstation',
    'open theatre': 'Open-air theatre',
    'open theater': 'Open-air theatre',
    'open-air theatre': 'Open-air theatre',
    'open air theatre': 'Open-air theatre',
    'open-air theater': 'Open-air theatre',
    'open air theater': 'Open-air theatre',
    # Single-sport play zones were filed under the sport's own name by the
    # vendor wizard (Box cricket above, these two as well) — they are Sports.
    'swimming pool': 'Play zone',
    'pickleball': 'Play zone',
    'gaming': 'Playstation',
    'gaming zone': 'Playstation',
    'gym': 'Gym',
    'gyms': 'Gym',
    'fitness': 'Gym',
    'fitness centre': 'Gym',
    'fitness center': 'Gym',
    'fitness studio': 'Gym',
    'gym & fitness': 'Gym',
    'gym and fitness': 'Gym',
    'big screen surprise': 'Big screen surprise',
    'big-screen surprise': 'Big screen surprise',
    'big screen surprises': 'Big screen surprise',
    'cinema surprise': 'Big screen surprise',
}


# Legacy sub-category spellings vendors have actually typed -> canonical.
_SUBCATEGORY_ALIASES = {
    'bachelors party': 'Bachelor Party',
    'bachelorette party': 'Bachelor Party',
    'seminar / meeting': 'Seminar & Meetings',
    'seminar/meeting': 'Seminar & Meetings',
    'seminar and meetings': 'Seminar & Meetings',
    'meetings': 'Seminar & Meetings',
    'anniversary': 'Anniversary Celebration',
    'get together': 'Get Together Party',
    'football': 'Football Turf',
    'ps 5': 'PS5',
    # The Fitness list's earlier short names.
    'group classes': 'Group Fitness Classes',
    'cardio': 'Cardio Training',
    'crossfit': 'CrossFit / Functional Training',
}


def _normalise(text):
    """'Seminar / Meeting' -> 'seminar meeting' so punctuation and separators
    stop blocking otherwise obvious matches."""
    cleaned = ''.join(ch if ch.isalnum() else ' ' for ch in str(text).lower())
    return ' '.join(cleaned.split())


def canonical_category(raw):
    """Map any stored/typed category onto the canonical name.
    Unknown values are returned unchanged — never silently dropped."""
    text = str(raw or '').strip()
    if not text:
        return ''
    if text in CATEGORIES:
        return text
    return _CATEGORY_ALIASES.get(text.lower(), text)


def canonical_sub_category(category, raw):
    """
    Map a stored sub-category onto the canonical spelling WITHIN its category,
    so legacy short forms line up: a hall's 'Birthday' becomes 'Birthday
    Party', a theatre's becomes 'Birthday Celebration'. Unknown values pass
    through untouched.
    """
    text = str(raw or '').strip()
    if not text:
        return ''
    options = CATEGORIES.get(canonical_category(category), [])
    lowered = text.lower()
    for option in options:
        if option.lower() == lowered:
            return option

    aliased = _SUBCATEGORY_ALIASES.get(lowered)
    if aliased and aliased in options:
        return aliased

    normalised = _normalise(text)
    for option in options:
        if _normalise(option) == normalised:
            return option
    for option in options:
        candidate = _normalise(option)
        if candidate.startswith(normalised) or normalised in candidate:
            return option
    return text


MAX_CUSTOM_CATEGORY = 50   # matches the indexed Listing.category column


def resolve_category(record):
    """The category customers actually see and filter by.

    A vendor who picks "Others" types their own, e.g. "Rooftop Lounge", and
    that free text becomes the venue's real category — "Others" is a wizard
    choice, not something anyone wants to browse. Falls back to the stored
    category whenever the custom name is missing, so a half-filled draft can
    never publish a venue categorised as "Others".
    """
    record = record or {}
    detail = record.get('detail') or {}
    stored = str(record.get('category') or '').strip()

    primary = str(detail.get('primaryCategory') or '').strip()
    if primary.lower() == OTHERS.lower():
        custom = str(detail.get('customCategory') or '').strip()
        if custom:
            return custom[:MAX_CUSTOM_CATEGORY]
    return stored


def _sub_category_names(value):
    """Sub-categories arrive either as a list or as a {name: true} map —
    the wizard uses the map shape for vendor-typed ones. Only truthy keys
    count, so unticking a box removes it rather than leaving it behind."""
    if isinstance(value, dict):
        return [name for name, on in value.items() if on]
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def sub_categories_for(listing):
    """
    The flat, canonical sub-category list for a venue, gathered from wherever
    the vendor's data actually lives:
      - detail.subCategories  (what the wizard sends once it multi-selects)
      - detail.occasions      (halls / theatres today)
      - detail.sports[].name  (play zones today)
    De-duplicated, order preserved.
    """
    record = listing.record or {}
    detail = record.get('detail') or {}
    category = canonical_category(
        resolve_category(record) or record.get('category') or listing.category
    )

    # Names added with the wizard's "Others" option (detail.
    # customSubCategories) are shown only once the super admin approved them,
    # in their approved spelling — and never run through the fuzzy matcher
    # below, which would turn "Cricket" into "Box Cricket".
    from .subcategories import is_declared_custom, public_custom_names

    raw = []
    raw.extend(_sub_category_names(detail.get('subCategories')))
    raw.extend(_sub_category_names(record.get('subCategories')))
    raw.extend(_sub_category_names(detail.get('occasions')))
    raw.extend(
        entry.get('name') for entry in (detail.get('sports') or [])
        if isinstance(entry, dict)
    )

    seen, result = set(), []
    for value in raw:
        if is_declared_custom(detail, value):
            continue
        name = canonical_sub_category(category, value)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    for name in public_custom_names(category, detail):
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result


def category_group(category):
    """The primary category ('celebrate' | 'sports' | 'gaming' | 'resorts' |
    'fitness') a stored category belongs to, or '' (e.g. a vendor's own
    "Others" name — a gym typed there still counts as Fitness)."""
    canonical = canonical_category(category)
    group = _GROUP_OF.get(canonical.lower(), '')
    if not group and canonical:
        words = _normalise(canonical).split()
        if any(w in ('gym', 'gyms', 'fitness') for w in words):
            return 'fitness'
    return group


def taxonomy_payload():
    """[{name, subCategories:[...], group}] — the shape GET /api/config
    serves (`group` is additive)."""
    # Approved custom sub-categories (venues/subcategories.py) follow the
    # built-in ones, so the customer filters offer them too.
    try:
        from .subcategories import approved_names
        custom = approved_names()
    except Exception:  # noqa: BLE001 — the config must never fail over this
        custom = {}
    return [
        {
            'name': name,
            'subCategories': subs + [c for c in custom.get(name, []) if c not in subs],
            'group': _GROUP_OF.get(name.lower(), ''),
        }
        for name, subs in CATEGORIES.items()
    ]


def groups_payload():
    """The primary categories, in navigation order (GET /api/config)."""
    return [dict(group) for group in CATEGORY_GROUPS]
