import logging

from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User

from .completion import compute_completion
from .models import PayoutDetails, Venue, VenuePhoto
from .pagination import VenuePagination
from .serializers import (
    PayoutDetailsSerializer,
    VenueDetailSerializer,
    VenueListSerializer,
    VenuePhotoCreateSerializer,
    VenuePhotoSerializer,
    VenueUpdateSerializer,
)


class IsVendor(BasePermission):
    """Allow only logged-in users whose role is VENDOR."""

    message = 'Only vendor accounts can access this endpoint.'

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == User.Role.VENDOR
        )


def get_vendor_venue(request, pk):
    """
    Fetch ONE venue that belongs to the requesting vendor.

    SECURITY: filtering by vendor=request.user means a vendor asking for
    someone else's venue id gets a 404 — we never reveal that the venue
    even exists (prevents IDOR attacks). Soft-deleted venues 404 too.
    """
    return get_object_or_404(
        request.user.venues.filter(is_deleted=False).prefetch_related(
            'units', 'packages', 'sports', 'addons', 'photos'
        ),
        pk=pk,
    )


class VenueListCreateView(APIView):
    """
    GET  /api/v1/vendor/venues  -> my venues (paginated)
    POST /api/v1/vendor/venues  -> create an empty DRAFT venue
    """

    permission_classes = [IsVendor]

    def get(self, request):
        venues = (
            request.user.venues.filter(is_deleted=False)
            .prefetch_related('units', 'sports', 'photos')  # completion() reads these
            .order_by('-updated_at')
        )
        paginator = VenuePagination()
        page = paginator.paginate_queryset(venues, request)
        serializer = VenueListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        # The wizard starts from a blank draft — no fields required.
        venue = request.user.venues.create()
        return Response(
            {'id': venue.id, 'status': venue.status, 'completion': 0},
            status=status.HTTP_201_CREATED,
        )


class VenueDetailView(APIView):
    """
    GET   /api/v1/vendor/venues/<id> -> full venue incl. completion & missing
    PATCH /api/v1/vendor/venues/<id> -> save wizard progress (partial update)
    """

    permission_classes = [IsVendor]

    def get(self, request, pk):
        venue = get_vendor_venue(request, pk)
        return Response(VenueDetailSerializer(venue).data)

    def patch(self, request, pk):
        venue = get_vendor_venue(request, pk)

        # Editing is only allowed while drafting or fixing a rejection.
        if venue.status not in (Venue.Status.DRAFT, Venue.Status.REJECTED):
            return Response(
                {'detail': f'Venue cannot be edited while status is {venue.status}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = VenueUpdateSerializer(venue, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        if venue.status == Venue.Status.REJECTED:
            # Editing a rejected venue puts it back into DRAFT with a clean slate.
            venue = serializer.save(status=Venue.Status.DRAFT, rejection_reason='')
        else:
            venue = serializer.save()

        # get_vendor_venue() prefetched the old related rows; drop that cache
        # so completion is computed from the fresh (replaced) rows.
        venue._prefetched_objects_cache = {}

        completion, missing = compute_completion(venue)
        return Response({
            'id': venue.id,
            'completion': completion,
            'missing': missing,
            'saved_at': venue.updated_at,
        })

    def delete(self, request, pk):
        """DELETE /api/v1/vendor/venues/<id> — SOFT delete (hide, don't erase)."""
        venue = get_vendor_venue(request, pk)
        venue.is_deleted = True
        venue.save(update_fields=['is_deleted', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)


logger = logging.getLogger(__name__)


class VenueSubmitView(APIView):
    """
    POST /api/v1/vendor/venues/<id>/submit — send a finished draft
    to the admin team for review (DRAFT -> PENDING).
    """

    permission_classes = [IsVendor]

    def post(self, request, pk):
        venue = get_vendor_venue(request, pk)

        # Idempotent: submitting twice is not an error, just a no-op.
        if venue.status == Venue.Status.PENDING:
            return Response({'detail': 'Already submitted.'}, status=status.HTTP_200_OK)

        if venue.status == Venue.Status.LIVE:
            return Response(
                {'detail': 'Venue is already live.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if venue.status == Venue.Status.REJECTED:
            # Editing a rejected venue (PATCH) resets it to DRAFT first.
            return Response(
                {'detail': 'Venue was rejected. Edit it to fix the issues, then submit again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The SAME function that powers the progress circle decides
        # submission — the two can never disagree.
        _, missing = compute_completion(venue)
        if missing:
            return Response({'missing': missing}, status=status.HTTP_400_BAD_REQUEST)

        venue.status = Venue.Status.PENDING
        venue.save(update_fields=['status', 'updated_at'])

        # Placeholder for a real admin notification (email/dashboard) later.
        logger.info('ADMIN NOTIFICATION: venue %s pending', venue.id)

        return Response({
            'id': venue.id,
            'status': venue.status,
            'detail': 'Submitted for review.',
        })


class VenuePhotoCreateView(APIView):
    """POST /api/v1/vendor/venues/<id>/photos {image_url, type} -> 201."""

    permission_classes = [IsVendor]

    def post(self, request, pk):
        venue = get_vendor_venue(request, pk)

        serializer = VenuePhotoCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        photo_type = serializer.validated_data['type']
        existing = venue.photos.filter(type=photo_type)
        if existing.count() >= VenuePhoto.MAX_PER_TYPE:
            return Response(
                {'detail': f'Maximum {VenuePhoto.MAX_PER_TYPE} photos allowed for type {photo_type}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # New photo goes to the end of its gallery.
        photo = serializer.save(venue=venue, order=existing.count())
        return Response(VenuePhotoSerializer(photo).data, status=status.HTTP_201_CREATED)


class VenuePhotoDeleteView(APIView):
    """DELETE /api/v1/vendor/venues/<id>/photos/<pid> -> 204."""

    permission_classes = [IsVendor]

    def delete(self, request, pk, photo_id):
        venue = get_vendor_venue(request, pk)
        # Same 404-on-foreign-id rule applies to photos.
        photo = get_object_or_404(venue.photos, pk=photo_id)
        photo.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PayoutView(APIView):
    """
    GET /api/v1/vendor/payout -> saved details (masked) or all-null fields
    PUT /api/v1/vendor/payout -> validate & save (create or overwrite)
    """

    permission_classes = [IsVendor]

    FIELDS = [
        'account_holder', 'bank_name', 'account_number',
        'ifsc', 'payout_phone', 'upi_id', 'pan',
    ]

    def get(self, request):
        payout = getattr(request.user, 'payout_details', None)
        if payout is None:
            # Not saved yet — same keys, all null, so the frontend
            # form can bind without special-casing.
            return Response({field: None for field in self.FIELDS})
        return Response(PayoutDetailsSerializer(payout).data)

    def put(self, request):
        payout = getattr(request.user, 'payout_details', None)
        # Passing instance=None -> create; instance -> full overwrite.
        serializer = PayoutDetailsSerializer(payout, data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save(user=request.user)
        return Response(serializer.data, status=status.HTTP_200_OK)
