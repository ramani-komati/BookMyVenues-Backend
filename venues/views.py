"""
Venue-registration draft endpoints (frontend contract, Group 4).

All routes require a VENDOR JWT and are scoped to the token owner:
asking for someone else's draft id returns 404 (never reveals it exists).
Error shape everywhere: {"message": "..."}.
"""
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User

from .completion import compute_completion
from .draft_validation import SECTIONS, validate_section
from .models import VenueDraft, empty_draft_data


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
