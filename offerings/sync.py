"""
Record block → tables. Runs after every successful publish / admin edit
(see the module docstring of offerings/models.py).

Upsert by the form's id; anything the block no longer lists is ARCHIVED
(archived_at set, off sale) — never deleted, because memberships and
bookings point at it. A row that comes back in a later publish is restored.
"""
import datetime

from django.db import transaction
from django.utils import timezone

from .models import FitnessPlan, OfferingProfile, ResortPackage, SurpriseShow
from .schema import offering_kind

# What each kind's profile settings keep (the block minus its row lists).
_ROW_LISTS = {'fitness': 'plans', 'resort': 'packages', 'surprise': 'shows'}


def _day(text):
    return datetime.date.fromisoformat(text) if text else None


def _plan_fields(entry, order):
    return {
        'kind': entry['kind'], 'name': entry['name'], 'description': entry['description'],
        'enabled': entry['enabled'], 'sort_order': order,
        'price': entry['price'] or 0,
        'duration_value': entry['durationValue'], 'duration_unit': entry['durationUnit'],
        'start_rule': entry['startRule'], 'start_within_days': entry['startWithinDays'],
        'expiry_rule': entry['expiryRule'], 'visits': entry['visits'],
        'facilities': entry['facilities'],
        'trial_available': entry['trialAvailable'], 'trial_price': entry['trialPrice'],
        'joining_fee': entry['joiningFee'],
    }


def _package_fields(entry, order):
    return {
        'name': entry['name'], 'description': entry['description'],
        'kind': entry['kind'], 'duration': entry['duration'], 'pricing': entry['pricing'],
        'price': entry['price'] or 0, 'child_price': entry['childPrice'],
        'min_persons': entry['minPersons'], 'max_persons': entry['maxPersons'],
        'capacity': entry['capacity'], 'slots': entry['slots'],
        'check_in': entry['checkIn'], 'check_out': entry['checkOut'],
        'available_from': _day(entry['availableFrom']), 'available_to': _day(entry['availableTo']),
        'inclusions': entry['inclusions'] + ([entry['otherInclusion']] if entry['otherInclusion'] else []),
        'enabled': entry['enabled'], 'sort_order': order,
    }


def _show_fields(entry, order):
    return {
        'movie': entry['movie'], 'language': entry['language'], 'screen': entry['screen'],
        'times': entry['times'],
        'available_from': _day(entry['availableFrom']), 'available_to': _day(entry['availableTo']),
        'enabled': entry['enabled'], 'sort_order': order,
    }


_TABLES = {
    'fitness': (FitnessPlan, _plan_fields),
    'resort': (ResortPackage, _package_fields),
    'surprise': (SurpriseShow, _show_fields),
}


def _sync_rows(listing, model, fields_of, entries):
    now = timezone.now()
    existing = {row.key: row for row in model.objects.filter(listing=listing)}
    seen = set()
    for order, entry in enumerate(entries):
        key = entry['id']
        seen.add(key)
        fields = fields_of(entry, order)
        row = existing.get(key)
        if row is None:
            model.objects.create(listing=listing, key=key, **fields)
            continue
        for name, value in fields.items():
            setattr(row, name, value)
        row.archived_at = None               # listed again → back on sale
        row.save()
    model.objects.filter(listing=listing, archived_at__isnull=True).exclude(key__in=seen).update(
        archived_at=now, enabled=False,
    )


@transaction.atomic
def sync_listing(listing):
    """Bring the tables in line with the listing's (already cleaned) record.
    A record without an offering block leaves the tables as they are — a
    legacy venue simply has none."""
    record = listing.record or {}
    kind = offering_kind(record)
    detail = record.get('detail') if isinstance(record.get('detail'), dict) else {}
    block = detail.get(kind) if kind else None
    if not kind or not isinstance(block, dict):
        return None

    rows_key = _ROW_LISTS[kind]
    model, fields_of = _TABLES[kind]
    _sync_rows(listing, model, fields_of, block.get(rows_key) or [])

    settings = {k: v for k, v in block.items() if k != rows_key}
    profile, _ = OfferingProfile.objects.update_or_create(
        listing=listing, defaults={'kind': kind, 'settings': settings},
    )
    return profile
