"""
Super-admin panel endpoints (all under /api/admin/).

Auth is a 2-step COOKIE SESSION: password -> OTP (SMS via 2Factor) -> session.
Error shape everywhere: {"detail": "..."}.

Phase 1: auth + the aggregate `bootstrap` read. Writes and the new models
(payouts / reviews / audit / settings persistence) come in later phases.
"""
import datetime

from django.contrib.auth import login, logout
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import PhoneOTP, User
from accounts.otp import OTPSendError, generate_code, send_otp_sms
from bookings.models import Booking
from venues.models import Listing, VenueDraft

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from .formatters import (
    approval_row,
    booking_row,
    build_bootstrap,
    user_row,
    vendor_row,
    venue_row,
)

REVIEW_STATUSES = {'pending', 'approved', 'changes', 'rejected'}
BOOKING_STATUSES = {'confirmed', 'completed', 'refund_pending', 'refunded', 'cancelled'}

# The one backend the admin session is logged in with.
_BACKEND = 'django.contrib.auth.backends.ModelBackend'


def _admin_by_email(email):
    email = str(email or '').strip().lower()
    if not email:
        return None
    return User.objects.filter(email__iexact=email, role=User.Role.ADMIN).first()


class AdminLoginView(APIView):
    """POST /api/admin/auth/login {email, password} -> {"otpRequired": true}."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        admin = _admin_by_email(request.data.get('email'))
        password = str(request.data.get('password') or '')
        # Same generic message whether the email is unknown or the password is
        # wrong — never reveal which admin emails exist.
        if admin is None or not admin.check_password(password):
            return detail('Invalid email or password.', status.HTTP_400_BAD_REQUEST)

        code = generate_code()
        try:
            send_otp_sms(admin.phone, code)
        except OTPSendError:
            return detail(
                'Could not send the OTP right now. Please try again.',
                status.HTTP_502_BAD_GATEWAY,
            )

        # A new OTP invalidates any earlier unused admin OTP for this phone.
        PhoneOTP.objects.filter(
            phone=admin.phone, purpose=PhoneOTP.Purpose.ADMIN, used=False
        ).update(used=True)
        PhoneOTP.objects.create(
            phone=admin.phone,
            purpose=PhoneOTP.Purpose.ADMIN,
            code_hash=make_password(code),
            expires_at=timezone.now() + datetime.timedelta(minutes=PhoneOTP.LIFETIME_MINUTES),
        )
        return Response({'otpRequired': True})


class AdminVerifyOtpView(APIView):
    """POST /api/admin/auth/verify-otp {email, otp} -> {"token": ...} + cookie."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        admin = _admin_by_email(request.data.get('email'))
        code = str(request.data.get('otp') or '')
        if admin is None:
            return detail('That code is not right.', status.HTTP_400_BAD_REQUEST)

        otp = PhoneOTP.objects.filter(
            phone=admin.phone, purpose=PhoneOTP.Purpose.ADMIN, used=False
        ).first()
        if (
            otp is None
            or timezone.now() >= otp.expires_at
            or otp.attempts >= PhoneOTP.MAX_ATTEMPTS
            or not check_password(code, otp.code_hash)
        ):
            if otp is not None and otp.attempts < PhoneOTP.MAX_ATTEMPTS:
                otp.attempts += 1
                otp.save(update_fields=['attempts'])
            return detail('That code is not right.', status.HTTP_400_BAD_REQUEST)

        otp.used = True
        otp.verified = True
        otp.save(update_fields=['used', 'verified'])

        # Establish the session cookie.
        login(request, admin, backend=_BACKEND)
        return Response({'token': request.session.session_key or ''})


class AdminLogoutView(APIView):
    """POST /api/admin/auth/logout -> 204, clears the session cookie."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminBootstrapView(APIView):
    """GET /api/admin/bootstrap -> the whole panel in one call."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(build_bootstrap())


# ---------------------------------------------------------------
# Writes (Phase 2) — partial PATCH updates on existing entities.
# All are admin-only; each echoes the updated entity.
# ---------------------------------------------------------------

class _AdminWriteView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]

    def _body(self, request):
        return request.data if isinstance(request.data, dict) else {}


class AdminApprovalUpdateView(_AdminWriteView):
    """PATCH /api/admin/approvals/<id> — status / checks / notes / timeline."""

    def patch(self, request, draft_id):
        draft = VenueDraft.objects.filter(pk=draft_id).first()
        if draft is None:
            return detail('Approval not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value not in REVIEW_STATUSES:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
            draft.review_status = value
        if isinstance(data.get('checks'), dict):
            draft.review_checks = {**(draft.review_checks or {}), **data['checks']}
        if 'notes' in data:
            draft.review_notes = str(data['notes'] or '')
        if isinstance(data.get('timeline'), list):
            draft.review_timeline = data['timeline']

        draft.save()
        return Response(approval_row(draft))


class AdminVenueUpdateView(_AdminWriteView):
    """PATCH /api/admin/venues/<id> — status (live/paused) / featured."""

    def patch(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id).first()
        if listing is None:
            return detail('Venue not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value == 'live':
                listing.status = Listing.Status.LIVE
            elif value == 'paused':
                listing.status = Listing.Status.PAUSED
            else:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
        if 'featured' in data:
            listing.featured = bool(data['featured'])

        listing.save()
        return Response(venue_row(listing))


class AdminVendorUpdateView(_AdminWriteView):
    """PATCH /api/admin/vendors/<id> — kyc / acc (suspend/reactivate)."""

    def patch(self, request, vendor_id):
        vendor = User.objects.filter(pk=vendor_id, role=User.Role.VENDOR).first()
        if vendor is None:
            return detail('Vendor not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'kyc' in data:
            value = str(data['kyc'])
            if value not in {'verified', 'pending', 'rejected'}:
                return detail('Invalid kyc value.', status.HTTP_400_BAD_REQUEST)
            vendor.kyc = value
        if 'acc' in data:
            value = str(data['acc'])
            if value == 'suspended':
                vendor.is_active = False
            elif value == 'active':
                vendor.is_active = True
            else:
                return detail('Invalid acc value.', status.HTTP_400_BAD_REQUEST)

        vendor.save()
        return Response(vendor_row(vendor))


class AdminUserUpdateView(_AdminWriteView):
    """PATCH /api/admin/users/<id> — status (block/unblock)."""

    def patch(self, request, user_id):
        user = User.objects.filter(pk=user_id, role=User.Role.PUBLIC).first()
        if user is None:
            return detail('User not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value == 'blocked':
                user.is_active = False
            elif value == 'active':
                user.is_active = True
            else:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)

        user.save()
        return Response(user_row(user))


class AdminBookingUpdateView(_AdminWriteView):
    """PATCH /api/admin/bookings/<id> — status (e.g. refunded)."""

    def patch(self, request, booking_id):
        booking = Booking.objects.filter(pk=booking_id).first()
        if booking is None:
            return detail('Booking not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value not in BOOKING_STATUSES:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
            booking.status = value

        booking.save()
        return Response(booking_row(booking))
