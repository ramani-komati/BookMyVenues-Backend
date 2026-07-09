"""
ONE source of truth for the wizard progress.

compute_completion(venue) -> (completion_percent, missing_list)

Used by GET detail, PATCH responses, and the submit endpoint —
so the frontend progress circle ALWAYS matches what submit will say.

5 sections x 20% each. A section counts only when ALL its
required items are filled.
"""
import re

from .models import Venue, VenuePhoto


def _basic_info(venue):
    """Section 1: who/what is this venue."""
    missing = []
    if not venue.name.strip():
        missing.append('Venue name')
    if not re.fullmatch(r'\d{10}', venue.contact_phone or ''):
        missing.append('Contact phone (10 digits)')
    return missing


def _location(venue):
    """Section 2: where is it."""
    missing = []
    if not venue.address_line.strip():
        missing.append('Address line')
    if not re.fullmatch(r'\d{6}', venue.pincode or ''):
        missing.append('Pincode (6 digits)')
    if not venue.state.strip():
        missing.append('State')
    if not venue.maps_link.strip():
        missing.append('Google Maps link')
    return missing


def _category_and_features(venue):
    """Section 3: what kind of venue and what it offers."""
    missing = []
    if not venue.category:
        missing.append('Venue category')
    if not venue.amenities:
        missing.append('At least one amenity')
    return missing


def _spaces_and_pricing(venue):
    """Section 4: bookable spaces (category-aware rules)."""
    missing = []
    units = list(venue.units.all())

    if venue.category == Venue.Category.PLAYZONE:
        # Playzones are priced per sport, not per hall.
        if not venue.sports.exists():
            missing.append('At least one sport with pricing (Playzone)')
    elif not units:
        missing.append('At least one hall/screen/lawn (unit)')

    for unit in units:
        if not unit.max_persons:
            missing.append(f'Max persons for unit "{unit.label}"')
    return missing


def _photos_and_payout(venue):
    """Section 5: ready to be shown & paid."""
    missing = []
    if not venue.photos.filter(type=VenuePhoto.Type.VENUE).exists():
        missing.append('At least one venue photo')
    # PayoutDetails is a OneToOne on the vendor (User) — hasattr is the
    # standard way to test if the related row exists without an exception.
    if not hasattr(venue.vendor, 'payout_details'):
        missing.append('Payout details')
    return missing


SECTIONS = [
    _basic_info,
    _location,
    _category_and_features,
    _spaces_and_pricing,
    _photos_and_payout,
]


def compute_completion(venue):
    """Returns (percent 0-100 in steps of 20, list of human-readable gaps)."""
    missing = []
    completed_sections = 0
    for section_check in SECTIONS:
        section_missing = section_check(venue)
        if section_missing:
            missing.extend(section_missing)
        else:
            completed_sections += 1
    return completed_sections * 20, missing
