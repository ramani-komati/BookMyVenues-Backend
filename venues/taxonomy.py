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
        'Corporate Events', 'Farewell Party',
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
    # Proposed — product had no list for this one yet.
    'Open-air theatre': [
        'Movie Screening', 'Live Sports Screening', 'Birthday Celebration',
        'Anniversary Celebration', 'Marriage Proposal', 'Surprise Celebration',
        'Rooftop Party', 'Private Dining',
    ],
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
    'open theatre': 'Open-air theatre',
    'open theater': 'Open-air theatre',
    'open-air theatre': 'Open-air theatre',
    'open air theatre': 'Open-air theatre',
}


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
    for option in options:
        if option.lower().startswith(lowered) or lowered in option.lower():
            return option
    return text


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
    category = canonical_category(record.get('category') or listing.category)

    raw = []
    raw.extend(detail.get('subCategories') or [])
    raw.extend(record.get('subCategories') or [])
    raw.extend(detail.get('occasions') or [])
    raw.extend(
        entry.get('name') for entry in (detail.get('sports') or [])
        if isinstance(entry, dict)
    )

    seen, result = set(), []
    for value in raw:
        name = canonical_sub_category(category, value)
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def taxonomy_payload():
    """[{name, subCategories:[...]}] — the shape GET /api/config serves."""
    return [
        {'name': name, 'subCategories': subs}
        for name, subs in CATEGORIES.items()
    ]
