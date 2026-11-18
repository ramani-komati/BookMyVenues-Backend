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

LOCATION. The pin customers are measured against (Listing.latitude /
longitude) is derived from the record on save (venues/geo.py record_pin):
the exact pin in the maps link, else the confirmed map location. An edit
that MOVES that pin — or adds or clears it — must say so with
`locationConfirmed: true` (the admin saw it on the map), else 400
LOCATION_UNCONFIRMED: nothing is ever guessed into place. Typed coordinates
must be real and inside India (PIN_INVALID / PIN_OUTSIDE_INDIA), and typing
coordinates that disagree with the exact pin the maps link carries is
refused (PIN_CONFLICT) — the link's pin would silently win, so the admin
replaces the link (or clears the coordinates) instead.

Every save is audited (who, when, which fields — with old → new values for
the name, category and pin) and kept as a VenueEditRevision: the venue
before and after, and every photo the save took off it, so a correction can
be reviewed and a removed photo restored. Photo files are never deleted
from storage by an edit.
"""
import re

from django.db import transaction
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.draft_validation import validate_section
from venues.geo import (
    haversine_km, in_india, pin_from_maps_link, pin_info, place_text_from_maps_link,
    record_maps_link, record_pin, valid_coordinates,
)
from venues.models import Listing, VenueDraft
from venues.taxonomy import CATEGORIES, OTHERS, VENUE_TYPES, canonical_category
from venues.timings import TimingsError, from_draft, validate_timings
from venues.views import PRIVATE_RECORD_KEYS, _extract_listing_columns, _strip_private_keys

from .auth import CsrfExemptSessionAuthentication, IsAdmin
from .formatters import venue_row
from .models import VenueEditRevision

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


# Two pins closer than this are the same place (GPS noise / rounding).
SAME_PLACE_KM = 0.1
REVISIONS_SHOWN = 20


def _point(lat, lng):
    if lat is None or lng is None:
        return None
    return valid_coordinates(lat, lng)


def _moved(before, after):
    """True when two (lat, lng)-or-None points are not the same place."""
    if before is None or after is None:
        return before != after
    return haversine_km(before[0], before[1], after[0], after[1]) > 0.001


def _photo_urls(sections, record):
    """{url: gallery} of every photo a venue shows — the draft's galleries
    and the record's cover / gallery."""
    urls = {}
    for gallery, items in ((sections or {}).get('photos') or {}).items():
        for item in items or []:
            if isinstance(item, dict) and item.get('url'):
                urls.setdefault(str(item['url']), gallery)
    for url in [(record or {}).get('image')] + list((record or {}).get('gallery') or []):
        if url:
            urls.setdefault(str(url), 'venuePhotos')
    return urls


def revision_rows(listing):
    return [
        {
            'id': rev.pk,
            'admin': rev.admin,
            'at': rev.created_at.isoformat() if rev.created_at else None,
            'changed': rev.changed,
            'removedPhotos': rev.removed_photos,
        }
        for rev in VenueEditRevision.objects.filter(listing_id=str(listing.pk))[:REVISIONS_SHOWN]
    ]


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
        # The pin customers are measured against right now, and the place
        # name a pin-less maps link points at (search text to confirm).
        'location': {
            **pin_info(listing),
            'placeText': place_text_from_maps_link(record_maps_link(listing.record or {})),
        },
        'revisions': revision_rows(listing),
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


def _validate_location(location):
    """Typed coordinates must be a real point inside India, sent as a pair;
    a maps link's own exact pin must be inside India too."""
    raw_lat = str(location.get('latitude') if location.get('latitude') is not None else '').strip()
    raw_lng = str(location.get('longitude') if location.get('longitude') is not None else '').strip()
    if raw_lat or raw_lng:
        if not (raw_lat and raw_lng):
            raise EditError('Enter both the latitude and the longitude, or neither.', code='PIN_INVALID')
        point = valid_coordinates(raw_lat, raw_lng)
        if point is None:
            raise EditError('The latitude / longitude are not a valid map point.', code='PIN_INVALID')
        if not in_india(*point):
            raise EditError(
                'That map point is outside India — check that the latitude and longitude '
                'are not swapped.', code='PIN_OUTSIDE_INDIA',
            )
    link_pin = pin_from_maps_link(location.get('mapsLink'))
    if link_pin is not None and not in_india(*link_pin):
        raise EditError('The Google Maps link points outside India.', code='PIN_OUTSIDE_INDIA')


def typed_pin(location):
    location = location or {}
    lat, lng = location.get('latitude'), location.get('longitude')
    if lat in (None, '') or lng in (None, ''):
        return None
    return valid_coordinates(lat, lng)


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
        if key == 'location' and isinstance(value, dict):
            _validate_location(value)       # first, for its specific error codes
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
        if 'location' in sections and 'mapPin' not in (incoming.get('detail') or {}):
            # The form cleared the coordinates — the merge must not keep the
            # stored pin alive (the record mapper omits mapPin when empty).
            record['detail'].pop('mapPin', None)
        draft = VenueDraft.objects.select_for_update().filter(pk=listing.pk).first()
        stored_location = ((draft.data or {}).get('location') or {}) if draft is not None else {}
        new_location = sections.get('location')
        if new_location is not None:
            merged_location = {**stored_location, **new_location}
            link_pin = pin_from_maps_link(merged_location.get('mapsLink'))
            typed = typed_pin(merged_location)
            # Only checked when the admin touched the location: an older
            # draft that already disagrees must not block unrelated edits.
            touched = any(
                str(merged_location.get(k) or '') != str(stored_location.get(k) or '')
                for k in ('mapsLink', 'latitude', 'longitude')
            )
            if touched and link_pin and typed and haversine_km(*link_pin, *typed) > SAME_PLACE_KM:
                raise EditError(
                    'The Google Maps link already pins a different spot, and that pin is '
                    'what customers see. Replace the link with one for the correct place '
                    '(or clear the latitude / longitude) and save again.',
                    code='PIN_CONFLICT',
                )
        old_pin = _point(listing.latitude, listing.longitude)
        new_pin = record_pin(record)
        if _moved(old_pin, new_pin) and body.get('locationConfirmed') is not True:
            raise EditError(
                'This save changes where the venue is on the map. Check the map '
                'preview and confirm the location before saving.',
                code='LOCATION_UNCONFIRMED',
            )
        siblings = _siblings(listing)
        if siblings and resource_signature(record.get('detail')) != resource_signature(before.get('detail')):
            raise EditError(
                'Changing the number of pitches, courts, screens or halls adds or '
                'removes bookable listings, so the vendor makes that change from '
                'their dashboard. Keep the same number here.',
                code='RESOURCES_CHANGED', http_status=status.HTTP_409_CONFLICT,
            )

        before_sections = {}
        if draft is not None:
            data = dict(draft.data or {})
            before_sections = {key: data[key] for key in (*EDITABLE_SECTIONS, 'photos') if key in data}
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
            if 'mapPin' not in shared:
                # A cleared pin must clear on every pitch / screen too — they
                # are one physical place with the base venue.
                updated['detail'].pop('mapPin', None)
            _save_listing(sibling, _strip_private_keys(updated))

        from venues.public_views import invalidate_listing_cache
        invalidate_listing_cache(listing)
        for sibling in siblings:
            invalidate_listing_cache(sibling)
        changed = _changed_keys(before, record)
        if photos is not None:
            changed.append('photos')
        after_sections = {**before_sections, **sections}
        if photos is not None:
            after_sections['photos'] = photos
        kept = _photo_urls(after_sections, record)
        removed = [
            {'gallery': gallery, 'url': url}
            for url, gallery in _photo_urls(before_sections, before).items() if url not in kept
        ]
        VenueEditRevision.objects.create(
            listing_id=str(listing.pk),
            admin_id=str(getattr(admin, 'pk', '') or ''),
            admin=(getattr(admin, 'name', '') or getattr(admin, 'phone', '')) if admin else '',
            changed=changed,
            before_record=before,
            after_record=record,
            before_sections=before_sections,
            removed_photos=removed,
        )
    listing.refresh_from_db()
    summary = {
        'changed': changed,
        'removed': removed,
        'name': (before.get('name'), record.get('name')),
        'category': (before.get('category'), record.get('category')),
        'pin': (old_pin, _point(listing.latitude, listing.longitude)),
    }
    return editable_payload(listing), summary


def _pin_text(point):
    return f'{point[0]:.6f},{point[1]:.6f}' if point else 'none'


AUDIT_LIMIT = 250        # AuditEntry.change is 300 chars; record_audit may append a reason


def audit_change(summary):
    """The audit line: old → new for what matters most, then the rest of
    the changed keys. The full before / after is in VenueEditRevision."""
    parts = []
    for key in ('name', 'category'):
        old, new = summary[key]
        if old != new:
            parts.append(f'{key}: {old or "—"} → {new or "—"}')
    old_pin, new_pin = summary['pin']
    if _moved(old_pin, new_pin):
        parts.append(f'pin: {_pin_text(old_pin)} → {_pin_text(new_pin)}')
    if summary['removed']:
        parts.append(f'{len(summary["removed"])} photo(s) removed')
    rest = [k for k in summary['changed'] if k not in ('name', 'category')]
    if rest:
        parts.append('changed: ' + ', '.join(rest))
    text = ' · '.join(parts) or 'no changes'
    return text if len(text) <= AUDIT_LIMIT else text[:AUDIT_LIMIT - 1] + '…'


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
            payload, summary = apply_edit(listing, request.data, admin=request.user)
        except EditError as error:
            return Response({'detail': error.message, 'code': error.code}, status=error.http_status)
        from .views import record_audit
        record_audit(
            request, 'Edited venue details', payload['venue'].get('name') or str(listing_id),
            audit_change(summary), target_id=str(listing_id), subject=listing.vendor,
        )
        return Response(payload)
