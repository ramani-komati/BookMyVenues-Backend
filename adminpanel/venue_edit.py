"""
Super-admin venue editing — the Edit action on the All venues page.

    GET /api/admin/venues/<id>/edit   the venue's editable details
    PUT /api/admin/venues/<id>/edit   save them

WHAT IS EDITED. A venue's public record is built from its registration
draft (the vendor portal republishes the listing from the draft whenever the
draft changes — venues "self-heal"). An edit that changed only the listing
would therefore be silently reverted by the vendor's next dashboard visit.
So an admin edit writes BOTH, in one transaction:

  * the draft's basics / location / details sections (and photos, when
    sent) — merged key by key, so a field the request leaves out keeps its
    value;
  * the published listing (base venue + its pitch / screen listings),
    rebuilt from those sections by the panel's own copy of the vendor
    portal's mapper (listingCompose.js), then merged over the stored record.

BANK DETAILS ARE NOT EDITABLE HERE. The draft's `payout` section is never
returned by GET and never written by PUT; a request that carries a payout
section or any bank / KYC key anywhere is refused outright (400
BANK_DETAILS_READ_ONLY), so a crafted request cannot slip one in. Bank
details keep their own workflow (the vendor's payout step).

SERVER-OWNED fields are never taken from the request: the listing's id,
owner, status, slug, featured flag, submittedAt, the vendor's pause state,
and the unit links (unitOf / unitLabel).

Changing how many pitches / screens / halls a venue has adds or removes
bookable listings, and removing one with bookings would strand customers —
that stays with the vendor (their publish flow checks upcoming bookings), so
it is refused here with 409 RESOURCES_CHANGED.

Concurrency: GET returns `version` (the listing's updated_at); PUT must send
it back. If the vendor (or another admin) saved in between, the PUT is
refused with 409 STALE_VERSION instead of overwriting their change.

Every save is audited (who, when, which fields).
"""
import re

from django.db import transaction
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.draft_validation import validate_section
from venues.models import Listing, VenueDraft
from venues.taxonomy import CATEGORIES, OTHERS, VENUE_TYPES, canonical_category
from venues.timings import TimingsError, from_draft, validate_timings
from venues.views import PRIVATE_RECORD_KEYS, _extract_listing_columns, _strip_private_keys

from .auth import CsrfExemptSessionAuthentication, IsAdmin
from .formatters import venue_row

EDITABLE_SECTIONS = ('basics', 'location', 'details')

# Record keys the listing keeps from what is ALREADY stored, whatever the
# request says (see the module docstring).
SERVER_OWNED_RECORD_KEYS = ('id', 'status', 'submittedAt', 'slug', 'featured', 'paused', 'pauseReason', 'meta')
SERVER_OWNED_DETAIL_KEYS = ('unitOf', 'unitLabel', 'paused', 'pauseReason')
# The draft's own pause fields are the vendor's too.
SERVER_OWNED_DETAILS_KEYS = ('paused', 'pauseReason')

MAX_NAME = 200
MAX_TEXT = 5000
MAX_PHOTOS = 30
_URL_RE = re.compile(r'^https://[^\s<>"\']+$', re.IGNORECASE)


class EditError(Exception):
    def __init__(self, message, code='INVALID', http_status=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.message, self.code, self.http_status = message, code, http_status


def _private(key):
    return str(key).replace('_', '').replace('-', '').lower() in PRIVATE_RECORD_KEYS


def find_private_keys(value, path=''):
    """Every bank / payout / KYC key anywhere inside `value` (dotted paths)."""
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            where = f'{path}.{key}' if path else str(key)
            if _private(key):
                found.append(where)
            else:
                found.extend(find_private_keys(inner, where))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            found.extend(find_private_keys(inner, f'{path}[{index}]'))
    return found


def _is_unit(listing):
    detail_ = (listing.record or {}).get('detail') or {}
    ref = str(detail_.get('unitOf') or '').strip() if isinstance(detail_, dict) else ''
    return bool(ref) and ref != str(listing.pk)


def _siblings(listing):
    return list(
        Listing.objects.filter(record__detail__unitOf=str(listing.pk), vendor_id=listing.vendor_id)
        .exclude(pk=listing.pk).exclude(status=Listing.Status.DELETED)
    )


def resource_signature(detail_):
    """What decides how many bookable listings a venue has: its pitches per
    sport, or its screens / halls / lawns."""
    detail_ = detail_ or {}
    sports = sorted(
        (str(s.get('name')), int(s.get('units') or 1))
        for s in detail_.get('sports') or [] if isinstance(s, dict) and s.get('name')
    )
    if sports:
        return ('sports', tuple(sports))
    try:
        units = int(detail_.get('unitCount') or 1)
    except (TypeError, ValueError):
        units = 1
    return ('units', units)


def version_of(listing):
    return listing.updated_at.isoformat() if listing.updated_at else ''


def editable_payload(listing):
    """GET shape. Never includes the draft's payout section."""
    draft = VenueDraft.objects.filter(pk=listing.pk).first()
    sections = None
    if draft is not None:
        data = draft.data or {}
        sections = {key: data.get(key) or {} for key in EDITABLE_SECTIONS}
        sections['photos'] = data.get('photos') or {'venuePhotos': [], 'serviceImages': []}
    vendor = listing.vendor
    return {
        'venue': venue_row(listing),
        'vendor': {
            'id': vendor.id if vendor else None,
            'name': getattr(vendor, 'name', '') or '',
            'phone': getattr(vendor, 'phone', '') or '',
        },
        # The published record is public-safe (bank keys are stripped at
        # publish), and lets the panel rebuild a form for a venue whose
        # draft no longer exists.
        'record': listing.record or {},
        'draft': sections,
        'draftStatus': draft.status if draft is not None else None,
        'version': version_of(listing),
        'siblingCount': len(_siblings(listing)),
        'bankDetailsEditable': False,
    }


def _text(value, label, limit, required=False):
    text = str(value or '').strip()
    if required and not text:
        raise EditError(f'{label} is required.')
    if len(text) > limit:
        raise EditError(f'{label} can be at most {limit} characters.')
    return text


def _url(value, label):
    text = str(value or '').strip()
    if text and not _URL_RE.match(text):
        raise EditError(f'{label} must be an https:// image link.')
    return text


def _validate_photos(photos):
    if photos is None:
        return None
    if not isinstance(photos, dict):
        raise EditError('Photos must be an object.')
    out = {}
    from venues.views import GALLERY_CAPS
    for gallery in GALLERY_CAPS:
        if gallery not in ('venuePhotos', 'serviceImages') and gallery not in photos:
            continue      # the Big Screen Surprise galleries are optional
        items = photos.get(gallery) or []
        if not isinstance(items, list):
            raise EditError('Photos must be lists.')
        if len(items) > MAX_PHOTOS:
            raise EditError(f'At most {MAX_PHOTOS} photos per gallery.')
        cleaned = []
        for item in items:
            if not isinstance(item, dict) or not item.get('url'):
                raise EditError('Each photo needs a url.')
            cleaned.append({**item, 'url': _url(item['url'], 'A photo')})
        out[gallery] = cleaned
    if not out['venuePhotos']:
        raise EditError('Keep at least one venue photo.')
    return out


def validate_edit(body):
    """(sections, photos, record) — validated, or raises EditError."""
    if not isinstance(body, dict):
        raise EditError('Request body must be an object.')
    sections = body.get('sections')
    record = body.get('record')
    if not isinstance(sections, dict) or not isinstance(record, dict):
        raise EditError('Send the edited sections and the rebuilt record.')

    # Bank details: refused, not silently dropped — a request that tries is
    # either a bug or an attack, and either way the admin should know.
    if 'payout' in sections or find_private_keys(sections) or find_private_keys(record):
        raise EditError(
            'Bank and payout details cannot be edited here. They are managed '
            'through the vendor’s payout details.',
            code='BANK_DETAILS_READ_ONLY',
        )
    unknown = set(sections) - set(EDITABLE_SECTIONS) - {'photos'}
    if unknown:
        raise EditError(f'Unknown section: {", ".join(sorted(unknown))}.')

    cleaned = {}
    for key in EDITABLE_SECTIONS:
        value = sections.get(key)
        if value is None:
            continue
        error = validate_section(key, value)
        if error:
            raise EditError(error)
        cleaned[key] = dict(value)

    basics = cleaned.get('basics')
    if basics is not None:
        _text(basics.get('venueName'), 'Venue name', MAX_NAME, required=True)
        _text(basics.get('shortDescription'), 'Short description', MAX_TEXT)
    details = cleaned.get('details')
    if details is not None:
        venue_type = str(details.get('primaryCategory') or '').strip()
        if venue_type not in VENUE_TYPES:
            raise EditError('Choose a valid category for the venue.')
        if venue_type == OTHERS:
            _text(details.get('customCategory'), 'Category name', 50, required=True)
        _text(details.get('fullDescription'), 'Description', MAX_TEXT)
        try:
            timings = validate_timings(from_draft(details))
        except TimingsError as error:
            raise EditError(str(error)) from error
        if timings is None:
            raise EditError('Set the venue timings (open and close time, or Open 24 hours).')

    photos = _validate_photos(sections.get('photos'))

    _text(record.get('name'), 'Venue name', MAX_NAME, required=True)
    category = str(record.get('category') or '').strip()
    known = canonical_category(category) in CATEGORIES
    if not known and str((details or {}).get('primaryCategory')) != OTHERS:
        raise EditError('The venue category is not one the platform offers.')
    price = str(record.get('price') or '').strip()
    if price:
        try:
            if float(price.replace(',', '')) < 0:
                raise ValueError
        except ValueError as error:
            raise EditError('Price must be a positive number.') from error
    _url(record.get('image'), 'The cover photo')
    for item in record.get('gallery') or []:
        _url(item, 'A gallery photo')
    record_detail = record.get('detail')
    if record_detail is not None and not isinstance(record_detail, dict):
        raise EditError('detail must be an object.')
    if isinstance(record_detail, dict) and 'timings' in record_detail:
        try:
            record_detail['timings'] = validate_timings(record_detail.get('timings'))
        except TimingsError as error:
            raise EditError(str(error)) from error
    # Membership plans / resort packages / surprise shows — the same check a
    # vendor publish runs (offerings/schema.py).
    from offerings.schema import OfferingError, clean_record
    try:
        record = clean_record(record)
    except OfferingError as error:
        raise EditError(str(error)) from error
    return cleaned, photos, record


def _merged_record(existing, incoming):
    """The stored record with the edit laid over it — server-owned keys and
    anything the edit does not mention keep their stored values."""
    stored = dict(existing or {})
    stored_detail = dict(stored.get('detail') or {})
    merged = {**stored, **{k: v for k, v in incoming.items() if k != 'detail'}}
    merged_detail = {**stored_detail, **(incoming.get('detail') or {})}
    for key in SERVER_OWNED_RECORD_KEYS:
        if key in stored:
            merged[key] = stored[key]
        else:
            merged.pop(key, None)
    for key in SERVER_OWNED_DETAIL_KEYS:
        if key in stored_detail:
            merged_detail[key] = stored_detail[key]
        else:
            merged_detail.pop(key, None)
    merged['detail'] = merged_detail
    if not merged.get('gallery'):
        merged['gallery'] = stored.get('gallery', [])
    return _strip_private_keys(merged)


def _changed_keys(before, after, prefix=''):
    keys = []
    for key in sorted(set(before) | set(after)):
        if key == 'detail' and isinstance(before.get(key), dict) and isinstance(after.get(key), dict):
            keys.extend(_changed_keys(before[key], after[key], 'detail.'))
        elif before.get(key) != after.get(key):
            keys.append(f'{prefix}{key}')
    return keys


def _save_listing(listing, record):
    from venues.taxonomy import resolve_category
    resolved = resolve_category(record)
    if resolved:
        record['category'] = resolved
    for field, value in _extract_listing_columns(record).items():
        setattr(listing, field, value)
    listing.record = record
    listing.save()


def apply_edit(listing, body, admin=None):
    """Validate and save; returns (payload, changed_keys). `admin` is the
    super admin making the edit (custom sub-categories they add are
    approved at once — venues/subcategories.py)."""
    sections, photos, incoming = validate_edit(body)
    with transaction.atomic():
        from venues.subcategories import SubCategoryError, register_from_details
        try:
            register_from_details(
                sections.get('details') or {}, user=admin, by_admin=True,
                reviewer=(getattr(admin, 'name', '') or getattr(admin, 'phone', '')) if admin else '',
            )
        except SubCategoryError as error:
            raise EditError(str(error)) from error
        listing = Listing.objects.select_for_update().get(pk=listing.pk)
        if str(body.get('version') or '') != version_of(listing):
            raise EditError(
                'This venue was changed since you opened it. Reload to see the '
                'latest details, then make your edit again.',
                code='STALE_VERSION', http_status=status.HTTP_409_CONFLICT,
            )
        before = dict(listing.record or {})
        record = _merged_record(before, incoming)
        siblings = _siblings(listing)
        if siblings and resource_signature(record.get('detail')) != resource_signature(before.get('detail')):
            raise EditError(
                'Changing the number of pitches, courts, screens or halls adds or '
                'removes bookable listings, so the vendor makes that change from '
                'their dashboard. Keep the same number here.',
                code='RESOURCES_CHANGED', http_status=status.HTTP_409_CONFLICT,
            )

        draft = VenueDraft.objects.select_for_update().filter(pk=listing.pk).first()
        if draft is not None:
            data = dict(draft.data or {})
            for key, value in sections.items():
                merged = {**(data.get(key) or {}), **value}
                if key == 'details':
                    for owned in SERVER_OWNED_DETAILS_KEYS:
                        if owned in (data.get(key) or {}):
                            merged[owned] = data[key][owned]
                        else:
                            merged.pop(owned, None)
                data[key] = merged
            if photos is not None:
                data['photos'] = photos
            draft.data = data          # payout is carried over untouched
            draft.save()

        _save_listing(listing, record)
        from offerings.sync import sync_listing
        sync_listing(listing)
        shared = {k: v for k, v in record['detail'].items() if k not in SERVER_OWNED_DETAIL_KEYS}
        for sibling in siblings:
            own = dict(sibling.record or {})
            own_detail = dict(own.get('detail') or {})
            label = own_detail.get('unitLabel') or (sibling.name.rsplit(' — ', 1)[-1] if ' — ' in sibling.name else '')
            updated = {
                **own,
                **{k: record[k] for k in ('category', 'locality', 'city', 'district', 'location', 'unit') if k in record},
                'name': f'{record["name"]} — {label}' if label else own.get('name'),
                'detail': {**own_detail, **shared},
            }
            _save_listing(sibling, _strip_private_keys(updated))

        from venues.public_views import invalidate_listing_cache
        invalidate_listing_cache(listing)
        for sibling in siblings:
            invalidate_listing_cache(sibling)
        changed = _changed_keys(before, record)
        if photos is not None:
            changed.append('photos')
    listing.refresh_from_db()
    return editable_payload(listing), changed


class AdminVenueEditView(APIView):
    """GET / PUT /api/admin/venues/<id>/edit — see the module docstring."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    # JSON only — admin writes are CSRF-exempt, so the JSON content type is
    # what forces the CORS preflight (see views._AdminWriteView).
    parser_classes = [JSONParser]

    def _listing(self, listing_id):
        listing = Listing.objects.select_related('vendor').filter(pk=listing_id).first()
        if listing is None:
            raise EditError('Venue not found.', 'NOT_FOUND', status.HTTP_404_NOT_FOUND)
        if listing.status == Listing.Status.DELETED:
            raise EditError('Deleted venues can no longer be edited.', 'DELETED', status.HTTP_409_CONFLICT)
        if _is_unit(listing):
            raise EditError(
                'This is one pitch / screen of a venue — edit the venue itself.',
                'UNIT_LISTING', status.HTTP_409_CONFLICT,
            )
        return listing

    def get(self, request, listing_id):
        try:
            listing = self._listing(listing_id)
        except EditError as error:
            return Response({'detail': error.message, 'code': error.code}, status=error.http_status)
        return Response(editable_payload(listing))

    def put(self, request, listing_id):
        try:
            listing = self._listing(listing_id)
            payload, changed = apply_edit(listing, request.data, admin=request.user)
        except EditError as error:
            return Response({'detail': error.message, 'code': error.code}, status=error.http_status)
        from .views import record_audit
        record_audit(
            request, 'Edited venue details', payload['venue'].get('name') or str(listing_id),
            ('changed: ' + ', '.join(changed[:20]) + (' …' if len(changed) > 20 else '')) if changed else 'no changes',
            target_id=str(listing_id), subject=listing.vendor,
        )
        return Response(payload)
