"""
Custom sub-categories — the names vendors and the super admin add with the
registration wizard's "Others" option when the built-in list
(venues/taxonomy.CATEGORIES) lacks one, e.g. Sports → "Cricket Nets".

Every rule lives here, so the vendor wizard, the super admin's Register
Venues form, the All venues editor and the public catalogue all agree:

  * ONE table (CustomSubCategory), keyed by the canonical stored category
    ('Play zone', 'Party hall'…) — the same source of truth for vendors and
    the super admin.
  * Names are tidied (trimmed, inner spaces collapsed) and de-duplicated by a
    key that ignores case, spacing and punctuation, so "cricket  NETS" and
    "Cricket-Nets" are one sub-category. A name that is really a built-in
    one ("football" → Football Turf) is never stored as custom.
  * A vendor's new name is PENDING until the super admin approves it; a name
    the super admin adds (directly, or while registering / editing a venue)
    is APPROVED at once. A REJECTED name cannot be re-submitted by a vendor.
  * Customers (GET /api/config, /api/venues…) and other vendors only ever
    see APPROVED names.

How a venue declares its custom names: the wizard keeps them in the draft's
`details.customSubCategories` (and ticks them in the usual
`subCategories` / `sports` map so pricing and display work unchanged); the
published record carries the same list as `detail.customSubCategories`.
"""
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone

from .taxonomy import (
    CATEGORIES,
    OTHERS,
    VENUE_TYPES,
    _SUBCATEGORY_ALIASES,
    _normalise,
    canonical_category,
)

MAX_NAME = 40
MIN_NAME = 2
MAX_PER_VENUE = 10

# Wizard venue types whose form has a sub-category list to extend (the
# CategoryPanel grids). Theatres and resorts have none; "Others" venues type
# their own free-form sub-categories already.
SUPPORTED_TYPES = ('Private Hall', 'Playzone', 'Playstation', 'Gym', 'Big Screen Surprise')
SUPPORTED_CATEGORIES = tuple(VENUE_TYPES[t] for t in SUPPORTED_TYPES)

# Plain words and the punctuation real sub-category names use
# ("Seminar & Meetings", "CrossFit / Functional Training", "Kids' Party").
# Deliberately excludes '·' and '—': the pitch listings are named
# "<venue> — <sport> · Unit 2", so a sport name must never contain them.
_ALLOWED_PUNCTUATION = set(" &/-'.(),+")
_RESERVED = {'other', 'others', 'none', 'na', 'n a'}

_CACHE_KEY = 'venues:custom-subcategories:v1'
_CACHE_SECONDS = 60


class SubCategoryError(ValueError):
    """A custom sub-category that cannot be accepted — the message is shown
    to the vendor / admin as-is."""


def clean_name(raw):
    """The tidy display name, or SubCategoryError when it is empty, too long
    or not a plausible name."""
    name = ' '.join(str(raw or '').split())
    if not name:
        raise SubCategoryError('Enter a sub-category name.')
    if len(name) < MIN_NAME:
        raise SubCategoryError('A sub-category name needs at least 2 characters.')
    if len(name) > MAX_NAME:
        raise SubCategoryError(f'Keep the sub-category name under {MAX_NAME} characters.')
    if not any(ch.isalpha() for ch in name):
        raise SubCategoryError('A sub-category name must contain letters.')
    if any(not (ch.isalnum() or ch in _ALLOWED_PUNCTUATION) for ch in name):
        raise SubCategoryError(
            'Use letters, numbers, spaces and & / - \' . ( ) + , only in a sub-category name.'
        )
    if _normalise(name) in _RESERVED:
        raise SubCategoryError('Type the actual sub-category name instead of "Others".')
    return name


def name_key(name):
    """The duplicate key: case, spacing and punctuation ignored."""
    return _normalise(name)[:MAX_NAME]


def category_for_type(venue_type):
    """Wizard venue type ('Playzone') or stored category ('Play zone') →
    the canonical category custom names attach to, or '' when it has no
    sub-category list to extend (theatres, resorts, "Others")."""
    text = str(venue_type or '').strip()
    if not text or text.lower() == OTHERS.lower():
        return ''
    category = VENUE_TYPES.get(text) or canonical_category(text)
    return category if category in SUPPORTED_CATEGORIES else ''


def builtin_match(category, name):
    """The built-in sub-category this name already is, else ''. Exact matches
    only (spelling, known aliases) — never the catalogue's fuzzy prefix match,
    so "Cricket" is not swallowed by "Box Cricket"."""
    options = CATEGORIES.get(category, [])
    key = name_key(name)
    for option in options:
        if name_key(option) == key:
            return option
    aliased = _SUBCATEGORY_ALIASES.get(str(name).strip().lower())
    if aliased in options:
        return aliased
    return ''


# ---------------------------------------------------------------------------
# Reading the shared list
# ---------------------------------------------------------------------------

def _snapshot():
    """{'approved': {category: [names]}, 'status': {(category, key): status}}
    — cached briefly because the public catalogue reads it per venue."""
    data = cache.get(_CACHE_KEY)
    if data is not None:
        return data
    from .models import CustomSubCategory
    approved, statuses = {}, {}
    for row in CustomSubCategory.objects.all().order_by('category', 'name'):
        statuses[f'{row.category}|{row.name_key}'] = row.status
        if row.status == CustomSubCategory.Status.APPROVED:
            approved.setdefault(row.category, []).append(row.name)
    data = {'approved': approved, 'status': statuses}
    cache.set(_CACHE_KEY, data, _CACHE_SECONDS)
    return data


def invalidate():
    cache.delete(_CACHE_KEY)


def approved_names(category=None):
    """Approved custom names — {category: [names]}, or one category's list."""
    approved = _snapshot()['approved']
    if category is None:
        return {k: list(v) for k, v in approved.items()}
    return list(approved.get(category, []))


def status_of(category, name):
    """'approved' | 'pending' | 'rejected' | '' (never registered)."""
    return _snapshot()['status'].get(f'{category}|{name_key(name)}', '')


def declared_custom(detail):
    """The names a draft's details / a record's detail declare as custom."""
    raw = (detail or {}).get('customSubCategories') if isinstance(detail, dict) else None
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(v) for v in raw if isinstance(v, str) and v.strip()]


def public_custom_names(category, detail):
    """The venue's custom names a CUSTOMER may see: approved ones only, in
    their approved spelling."""
    approved = {name_key(n): n for n in approved_names(category)}
    seen, result = set(), []
    for raw in declared_custom(detail):
        key = name_key(raw)
        if key in approved and key not in seen:
            seen.add(key)
            result.append(approved[key])
    return result


def is_declared_custom(detail, name):
    keys = {name_key(n) for n in declared_custom(detail)}
    return name_key(name) in keys


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def _approve(row, reviewer):
    from .models import CustomSubCategory
    row.status = CustomSubCategory.Status.APPROVED
    row.reviewed_by = reviewer
    row.reviewed_at = timezone.now()
    row.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])


def ensure(category, raw_name, *, user=None, by_admin=False, reviewer=''):
    """Find or create one custom sub-category. Returns (row, created) — row
    is None when the name is really a built-in one.

    by_admin: the super admin is adding / using it, so it is approved (a
    pending or rejected one included — the admin chose it). A vendor's new
    name starts pending; a vendor re-using a rejected one is refused.
    """
    from .models import CustomSubCategory

    if category not in SUPPORTED_CATEGORIES:
        raise SubCategoryError('This category has no sub-categories to add to.')
    name = clean_name(raw_name)
    if builtin_match(category, name):
        return None, False
    key = name_key(name)

    row = CustomSubCategory.objects.filter(category=category, name_key=key).first()
    created = False
    if row is None:
        try:
            with transaction.atomic():
                row = CustomSubCategory.objects.create(
                    category=category, name=name, name_key=key,
                    status=(CustomSubCategory.Status.APPROVED if by_admin
                            else CustomSubCategory.Status.PENDING),
                    created_by=user if getattr(user, 'pk', None) else None,
                    created_by_admin=by_admin,
                    reviewed_by=reviewer if by_admin else '',
                    reviewed_at=timezone.now() if by_admin else None,
                )
                created = True
        except IntegrityError:
            # Someone registered the same name a moment ago.
            row = CustomSubCategory.objects.get(category=category, name_key=key)

    if not created:
        if by_admin and row.status != CustomSubCategory.Status.APPROVED:
            _approve(row, reviewer)
        elif not by_admin and row.status == CustomSubCategory.Status.REJECTED:
            raise SubCategoryError(
                f'"{row.name}" was not accepted as a sub-category. '
                'Remove it and choose another.'
            )
    invalidate()
    return row, created


def register_from_details(details, *, user=None, by_admin=False, reviewer=''):
    """Record every custom name a draft's `details` declares (on submit, and
    when the admin edits a venue). Validates them all first, so a bad name
    leaves nothing half-written. Returns the rows."""
    names = declared_custom(details)
    if not names:
        return []
    category = category_for_type((details or {}).get('primaryCategory'))
    if not category:
        raise SubCategoryError(
            'Custom sub-categories can only be added under Celebrate (Private Hall), '
            'Sports, Gaming, Fitness or Big Screen Surprise.'
        )
    cleaned, seen = [], set()
    for raw in names:
        name = clean_name(raw)
        key = name_key(name)
        if key not in seen:
            seen.add(key)
            cleaned.append(name)
    if len(cleaned) > MAX_PER_VENUE:
        raise SubCategoryError(f'Add at most {MAX_PER_VENUE} custom sub-categories per venue.')
    rows = []
    with transaction.atomic():
        for name in cleaned:
            row, _ = ensure(category, name, user=user, by_admin=by_admin, reviewer=reviewer)
            if row is not None:
                rows.append(row)
    return rows


def acting_admin(request):
    """The admin behind a vendor token minted by POST /api/admin/vendors/token
    (its `actor` claim, 'admin:<id>') — the super admin's Register Venues
    flow — else None."""
    token = getattr(request, 'auth', None)
    try:
        actor = token.get('actor') if token is not None else None
    except AttributeError:
        return None
    if not isinstance(actor, str) or not actor.startswith('admin:'):
        return None
    from accounts.models import User
    try:
        admin_id = int(actor.split(':', 1)[1])
    except (TypeError, ValueError):
        return None
    admin = User.objects.filter(pk=admin_id, role=User.Role.ADMIN, is_active=True).first()
    return admin


def row_payload(row, *, for_admin=False):
    data = {
        'id': row.pk,
        'category': row.category,
        'name': row.name,
        'status': row.status,
    }
    if for_admin:
        creator = row.created_by
        data.update({
            'createdByAdmin': row.created_by_admin,
            'createdBy': (
                {'name': getattr(creator, 'name', '') or '', 'phone': getattr(creator, 'phone', '') or ''}
                if creator else None
            ),
            'reviewedBy': row.reviewed_by or None,
            'reviewedAt': row.reviewed_at.isoformat() if row.reviewed_at else None,
            'createdAt': row.created_at.isoformat(),
        })
    return data


def public_detail(category, detail):
    """A copy of a record's `detail` safe for customers: custom names that are
    not approved leave `occasions` and `customSubCategories` (approved ones
    take their approved spelling). `sports` is left alone — a sport is a
    bookable pitch with its own prices, and the venue itself is reviewed by
    the super admin before it goes live."""
    if not isinstance(detail, dict) or not declared_custom(detail):
        return detail
    visible = {name_key(n): n for n in public_custom_names(category, detail)}
    out = dict(detail)
    out['customSubCategories'] = list(visible.values())
    occasions = detail.get('occasions')
    if isinstance(occasions, list):
        declared = {name_key(n) for n in declared_custom(detail)}
        kept = []
        for value in occasions:
            key = name_key(value) if isinstance(value, str) else ''
            if key in declared:
                if key in visible:
                    kept.append(visible[key])
            else:
                kept.append(value)
        out['occasions'] = kept
    return out
