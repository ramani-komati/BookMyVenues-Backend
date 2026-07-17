"""
Venue-registration draft endpoints (frontend contract, Group 4).

All routes require a VENDOR JWT and are scoped to the token owner:
asking for someone else's draft id returns 404 (never reveals it exists).
Error shape everywhere: {"message": "..."}.
"""
import logging
import uuid

from django.utils import timezone
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User

from django.utils.text import slugify

from .completion import compute_completion
from .draft_validation import SECTIONS, validate_section
from .models import Listing, VenueDraft, empty_draft_data
from .storage import (
    ALLOWED_TYPES,
    MAX_FILE_SIZE,
    StorageError,
    delete_photo,
    upload_photo,
)

logger = logging.getLogger(__name__)

# Gallery name -> maximum number of photos (contract 4.4).
GALLERY_CAPS = {'venuePhotos': 5, 'serviceImages': 10}


class IsVendor(BasePermission):
    """Allow only logged-in users whose role is VENDOR."""

    message = 'Only vendor accounts can access this endpoint.'

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == User.Role.VENDOR
        )


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def get_vendor_draft(request, draft_id):
    """SECURITY: filtering by vendor=request.user makes foreign ids 404."""
    return get_object_or_404(request.user.drafts, pk=draft_id)


def _merge_sections(draft, body):
    """
    Shallow-merge any provided text sections into the draft's buckets.
    Returns an error message on bad input, else None.
    (Photos are managed by their own upload endpoints, never merged here.)
    """
    for section in SECTIONS:
        if section not in body:
            continue
        payload = body[section]
        error = validate_section(section, payload)
        if error:
            return error
        draft.data[section] = {**draft.data.get(section, {}), **payload}
    return None


def _draft_response(draft, include_status=False):
    payload = {
        'draftId': str(draft.id),
        'draft': draft.data,
        'completion': compute_completion(draft)[0],
        'savedAt': draft.updated_at,
    }
    if include_status:
        payload['status'] = draft.status
    return payload


class DraftCreateView(APIView):
    """POST /api/venues/drafts — start a draft (autosave bootstraps it)."""

    permission_classes = [IsVendor]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        draft = VenueDraft(vendor=request.user, data=empty_draft_data())

        error = _merge_sections(draft, body)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)

        draft.save()
        return Response(_draft_response(draft), status=status.HTTP_201_CREATED)


class DraftDetailView(APIView):
    """
    GET    /api/venues/drafts/<id> — resume a draft (page reload)
    DELETE /api/venues/drafts/<id> — "clear draft": wipe it completely
    """

    permission_classes = [IsVendor]

    def get(self, request, draft_id):
        draft = get_vendor_draft(request, draft_id)
        return Response(_draft_response(draft, include_status=True))

    def delete(self, request, draft_id):
        draft = get_vendor_draft(request, draft_id)
        draft.delete()
        return Response({'draftId': str(draft_id), 'deleted': True})


class DraftSectionView(APIView):
    """PATCH /api/venues/drafts/<id>/sections/<section> — debounced autosave."""

    permission_classes = [IsVendor]

    def patch(self, request, draft_id, section):
        draft = get_vendor_draft(request, draft_id)

        error = validate_section(section, request.data)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)

        # Shallow merge: only the keys sent are overwritten.
        draft.data[section] = {**draft.data.get(section, {}), **request.data}
        draft.save(update_fields=['data', 'updated_at'])

        return Response({
            'draftId': str(draft.id),
            'section': section,
            'completion': compute_completion(draft)[0],
            'savedAt': draft.updated_at,
        })


class DraftSubmitView(APIView):
    """
    POST /api/venues/drafts/<id>/submit — final submission.
    Runs the completeness gates; success -> status "pending".
    """

    permission_classes = [IsVendor]

    def post(self, request, draft_id):
        draft = get_vendor_draft(request, draft_id)

        # Idempotent: submitting an already-pending draft is not an error.
        if draft.status == VenueDraft.Status.PENDING:
            return Response({
                'draftId': str(draft.id),
                'status': draft.status,
                'submittedAt': draft.submitted_at,
            })

        _, missing = compute_completion(draft)
        if missing:
            return _message(
                'Missing: ' + ', '.join(missing),
                status.HTTP_400_BAD_REQUEST,
            )

        draft.status = VenueDraft.Status.PENDING
        draft.submitted_at = timezone.now()
        draft.save(update_fields=['status', 'submitted_at', 'updated_at'])

        return Response({
            'draftId': str(draft.id),
            'status': draft.status,
            'submittedAt': draft.submitted_at,
        })


class DraftReopenView(APIView):
    """POST /api/venues/drafts/<id>/reopen — Edit clicked: back to draft."""

    permission_classes = [IsVendor]

    def post(self, request, draft_id):
        draft = get_vendor_draft(request, draft_id)
        if draft.status != VenueDraft.Status.DRAFT:
            draft.status = VenueDraft.Status.DRAFT
            draft.save(update_fields=['status', 'updated_at'])
        return Response({'draftId': str(draft.id), 'status': draft.status})


class DraftSeedView(APIView):
    """
    POST /api/venues/drafts/<id>/seed — rebuild an editable draft UNDER THE
    LISTING'S ID from listing data, so a resubmit updates in place.
    Creates the draft if it doesn't exist; updates it if it does.
    """

    permission_classes = [IsVendor]

    def post(self, request, draft_id):
        draft = VenueDraft.objects.filter(pk=draft_id).first()

        if draft is not None and draft.vendor_id != request.user.id:
            # Someone else's draft — pretend it doesn't exist.
            return _message('Not found.', status.HTTP_404_NOT_FOUND)

        if draft is None:
            draft = VenueDraft(
                vendor=request.user, id=draft_id, data=empty_draft_data()
            )

        body = request.data if isinstance(request.data, dict) else {}
        error = _merge_sections(draft, body)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)

        draft.status = VenueDraft.Status.DRAFT
        draft.save()
        return Response({'draftId': str(draft.id), 'status': draft.status})


def _extract_listing_columns(record):
    """Pull the searchable fields out of the JSON record."""
    detail = record.get('detail') or {}
    return {
        'name': str(record.get('name') or '')[:200],
        'category': str(record.get('category') or '')[:50],
        'locality': str(record.get('locality') or '')[:120],
        'pincode': str(record.get('pincode') or detail.get('pincode') or '')[:10],
    }


def _unique_slug(name, listing_id):
    """URL-friendly name, e.g. 'grand-palace-hall'; falls back to the id."""
    base = slugify(str(name or ''))[:200] or str(listing_id)[:8]
    slug = base
    counter = 2
    while Listing.objects.filter(slug=slug).exclude(pk=listing_id).exists():
        slug = f'{base}-{counter}'
        counter += 1
    return slug


class VendorListingPublishView(APIView):
    """
    POST /api/vendors/me/listings — publish/update a listing from a
    submitted draft (contract 3.5).

    - Idempotent by id: publishing the same id again UPDATES the listing.
    - Owner is stamped from the token, never from the body.
    - If an update arrives without photos, the existing gallery is kept.
    - Status is "live" (auto-approve) until an admin review page exists.
    """

    permission_classes = [IsVendor]

    def post(self, request):
        record = request.data
        if not isinstance(record, dict):
            return _message('Request body must be a listing object.', status.HTTP_400_BAD_REQUEST)

        try:
            listing_id = uuid.UUID(str(record.get('id')))
        except (ValueError, TypeError):
            return _message('A valid listing id (the draftId) is required.', status.HTTP_400_BAD_REQUEST)

        existing = Listing.objects.filter(pk=listing_id).first()

        if existing is not None and existing.vendor_id != request.user.id:
            return _message('You do not own this listing.', status.HTTP_403_FORBIDDEN)

        if existing is None:
            # First publish: the id must come from the vendor's OWN draft —
            # nobody can squat an arbitrary id.
            if not request.user.drafts.filter(pk=listing_id).exists():
                return _message(
                    'You can only publish your own submitted draft.',
                    status.HTTP_403_FORBIDDEN,
                )

        record = dict(record)  # never mutate request.data itself
        # Contract: "keep existing photos if update has none".
        if existing is not None and not record.get('gallery'):
            record['gallery'] = existing.record.get('gallery', [])

        record['id'] = str(listing_id)
        record['status'] = Listing.Status.LIVE
        columns = _extract_listing_columns(record)

        if existing is not None:
            for field, value in columns.items():
                setattr(existing, field, value)
            existing.record = record
            existing.status = Listing.Status.LIVE
            existing.save()
            listing = existing
        else:
            listing = Listing.objects.create(
                id=listing_id,
                vendor=request.user,
                slug=_unique_slug(columns['name'], listing_id),
                record=record,
                status=Listing.Status.LIVE,
                **columns,
            )

        return Response({'listing': listing.record}, status=status.HTTP_201_CREATED)


class VendorListingDeleteView(APIView):
    """
    DELETE /api/vendors/me/listings/<id> — vendor removes a venue
    (contract 3.6). Blocked while upcoming bookings exist (409) so
    customers are never left with a reservation at a vanished venue.
    """

    permission_classes = [IsVendor]

    def delete(self, request, listing_id):
        listing = request.user.listings.filter(pk=listing_id).first()
        if listing is None:
            return _message('Listing not found.', status.HTTP_404_NOT_FOUND)

        # Import here to avoid a circular import at module load time.
        from bookings.models import Booking
        from bookings.slots import today_ist

        if Booking.objects.filter(listing=listing, date__gte=today_ist()).exists():
            return _message(
                'This venue has upcoming bookings. They must be cancelled '
                'before the venue can be deleted.',
                status.HTTP_409_CONFLICT,
            )

        listing.delete()  # past bookings survive (listing FK is SET_NULL)
        return Response({'deleted': True, 'id': str(listing_id)})


class DraftPhotoUploadView(APIView):
    """
    POST /api/venues/drafts/<id>/photos  (multipart/form-data)
    Fields: file (image), gallery (venuePhotos|serviceImages).
    Uploads to Supabase Storage -> permanent public URL.
    """

    permission_classes = [IsVendor]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, draft_id):
        draft = get_vendor_draft(request, draft_id)

        gallery = request.data.get('gallery')
        if gallery not in GALLERY_CAPS:
            return _message(
                'gallery must be "venuePhotos" or "serviceImages".',
                status.HTTP_400_BAD_REQUEST,
            )

        file = request.FILES.get('file')
        if file is None:
            return _message('No file uploaded.', status.HTTP_400_BAD_REQUEST)

        if file.content_type not in ALLOWED_TYPES:
            return _message(
                'Only JPEG, PNG or WebP images are allowed.',
                status.HTTP_400_BAD_REQUEST,
            )

        if file.size > MAX_FILE_SIZE:
            return _message(
                'Image is too large (max 5 MB).',
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        photos = draft.data.setdefault(
            'photos', {'venuePhotos': [], 'serviceImages': []}
        )
        existing = photos.setdefault(gallery, [])
        cap = GALLERY_CAPS[gallery]
        if len(existing) >= cap:
            return _message(
                f'Maximum {cap} photos allowed in {gallery}.',
                status.HTTP_400_BAD_REQUEST,
            )

        photo_id = uuid.uuid4().hex[:12]
        extension = ALLOWED_TYPES[file.content_type]
        path = f'{draft.id}/{gallery}/{photo_id}.{extension}'

        try:
            url = upload_photo(path, file.read(), file.content_type)
        except StorageError:
            logger.exception('Photo upload failed for draft %s', draft.id)
            return _message(
                'Could not store the photo right now. Please try again.',
                status.HTTP_502_BAD_GATEWAY,
            )

        # "path" is kept so delete can find the file in storage;
        # the frontend only cares about id/name/url and ignores extras.
        photo = {'id': photo_id, 'name': file.name, 'url': url, 'path': path}
        existing.append(photo)
        draft.save(update_fields=['data', 'updated_at'])

        return Response({
            'draftId': str(draft.id),
            'gallery': gallery,
            'photo': {'id': photo_id, 'name': file.name, 'url': url},
            'completion': compute_completion(draft)[0],
            'savedAt': draft.updated_at,
        }, status=status.HTTP_201_CREATED)


class DraftPhotoDeleteView(APIView):
    """DELETE /api/venues/drafts/<id>/photos/<photoId>?gallery=..."""

    permission_classes = [IsVendor]

    def delete(self, request, draft_id, photo_id):
        draft = get_vendor_draft(request, draft_id)

        gallery = request.query_params.get('gallery')
        if gallery not in GALLERY_CAPS:
            return _message(
                'gallery query parameter must be "venuePhotos" or "serviceImages".',
                status.HTTP_400_BAD_REQUEST,
            )

        photos = draft.data.get('photos', {}).get(gallery, [])
        photo = next((p for p in photos if p.get('id') == photo_id), None)
        if photo is None:
            return _message('Photo not found.', status.HTTP_404_NOT_FOUND)

        # Remove the stored file. Best-effort: if storage is briefly
        # unreachable we still remove it from the draft (an orphan file
        # is harmless; a ghost photo in the wizard is not).
        if photo.get('path'):
            try:
                delete_photo(photo['path'])
            except StorageError:
                logger.warning('Storage delete failed for %s', photo['path'])

        draft.data['photos'][gallery] = [p for p in photos if p.get('id') != photo_id]
        draft.save(update_fields=['data', 'updated_at'])

        return Response({
            'draftId': str(draft.id),
            'gallery': gallery,
            'photoId': photo_id,
            'completion': compute_completion(draft)[0],
            'savedAt': draft.updated_at,
        })
