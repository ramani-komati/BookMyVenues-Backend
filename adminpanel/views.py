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
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from accounts.models import PhoneOTP, User
from accounts.otp import OTPSendError, generate_code, send_otp_sms
from bookings.models import Booking
from venues.models import Listing, VenueDraft

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from .payouts import generate_payouts
from .formatters import (
    approval_row,
    audit_row,
    booking_row,
    build_bootstrap,
    payout_row,
    review_row,
    settings_row,
    user_row,
    vendor_row,
    venue_row,
)
from .models import AuditEntry, Payout, Review, Settings

REVIEW_STATUSES = {'pending', 'approved', 'changes', 'rejected'}
BOOKING_STATUSES = {'confirmed', 'completed', 'refund_pending', 'refunded', 'cancelled'}
PAYOUT_STATUSES = {'pending', 'failed', 'completed'}


def _to_int(value, default=0):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def record_audit(request, action, target, change='', target_id=''):
    """Write a server-side audit row. `target` is the entity's NAME (stays
    readable even if the entity is later deleted); `target_id` its raw id."""
    admin = getattr(request.user, 'name', '') or getattr(request.user, 'phone', '')
    AuditEntry.objects.create(
        admin=admin, action=action, target=str(target),
        target_id=str(target_id), change=str(change),
    )

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
    # The admin URL is public (DNS and certificate-transparency logs make any
    # subdomain discoverable), so the login itself must resist brute force —
    # obscurity is not a control.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

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
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

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
        generate_payouts()  # fill the Payouts page for completed weeks
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


# approval status -> the listing status it maps to.
_APPROVAL_TO_LISTING = {
    'approved': Listing.Status.LIVE,
    'pending': Listing.Status.PENDING,
    'changes': Listing.Status.CHANGES,
    'rejected': Listing.Status.REJECTED,
}


class AdminApprovalUpdateView(_AdminWriteView):
    """PATCH /api/admin/approvals/<id> — id is the LISTING id.

    Status changes cascade to the venue's unit siblings (the listings whose
    detail.unitOf points at this id) so approving the base venue makes its
    pitches/screens bookable too."""

    def patch(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id).first()
        if listing is None:
            return detail('Approval not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value not in REVIEW_STATUSES:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
            listing.status = _APPROVAL_TO_LISTING[value]
            # Cascade to the unit family (— Pitch 2 / Screen 3 siblings).
            Listing.objects.filter(
                record__detail__unitOf=str(listing.pk)
            ).update(status=listing.status)
        if isinstance(data.get('checks'), dict):
            listing.review_checks = {**(listing.review_checks or {}), **data['checks']}
        if 'notes' in data:
            listing.review_notes = str(data['notes'] or '')
        if isinstance(data.get('timeline'), list):
            listing.review_timeline = data['timeline']

        listing.save()
        record_audit(request, 'Approval update', listing.name, listing.status, target_id=str(listing.pk))
        return Response(approval_row(listing))


class AdminVenueUpdateView(_AdminWriteView):
    """PATCH /api/admin/venues/<id> — status (live/paused) / featured.

    Status changes apply to the venue's WHOLE listing set: the base row and
    its unit siblings (detail.unitOf family) move together, in both
    directions — pause hides all, live restores all."""

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
            # Cascade to the unit family — symmetric for pause AND unpause.
            Listing.objects.filter(
                record__detail__unitOf=str(listing.pk)
            ).update(status=listing.status)
        if 'featured' in data:
            listing.featured = bool(data['featured'])

        listing.save()
        change = f"{listing.status}{' · featured' if listing.featured else ''}"
        record_audit(request, 'Venue update', listing.name, change, target_id=str(listing.pk))
        return Response(venue_row(listing))


def _vendor_has_active_bookings(vendor):
    """True when any venue of the vendor has a confirmed, not-yet-completed
    booking (time-based: today's bookings count until their last slot ends)."""
    from bookings.slots import now_minutes_ist, slots_end_minute, today_ist

    today = today_ist()
    base = Booking.objects.filter(listing__vendor=vendor).exclude(
        status__in=('refunded', 'cancelled')
    )
    if base.filter(date__gt=today).exists():
        return True
    for booking in base.filter(date=today):
        end = slots_end_minute(booking.slots)
        if not end or end > now_minutes_ist():
            return True
    return False


class AdminVendorUpdateView(_AdminWriteView):
    """PATCH /api/admin/vendors/<id> — kyc / acc (suspend/reactivate).

    Suspending removes the vendor from the platform: refused (409) while any
    of their venues has an active booking; otherwise all their live venues
    (incl. unit siblings) are paused and the account is deactivated — which
    also blocks re-publishing, since inactive accounts fail authentication.
    Reactivating does NOT auto-relist: venues stay paused for manual review."""

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
                if _vendor_has_active_bookings(vendor):
                    return detail(
                        'Vendor has active bookings. Refund or complete them first.',
                        status.HTTP_409_CONFLICT,
                    )
                vendor.is_active = False
                vendor.listings.filter(status=Listing.Status.LIVE).update(
                    status=Listing.Status.PAUSED
                )
            elif value == 'active':
                vendor.is_active = True  # venues stay paused — unpause manually
            else:
                return detail('Invalid acc value.', status.HTTP_400_BAD_REQUEST)

        vendor.save()
        record_audit(request, 'Vendor update', vendor.name or vendor.phone,
                     f"kyc={vendor.kyc}, active={vendor.is_active}", target_id=str(vendor.pk))
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
        record_audit(request, 'User update', user.name or user.phone,
                     'active' if user.is_active else 'blocked', target_id=str(user.pk))
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
            if value == 'refunded' and booking.method in (
                Booking.Method.VENUE, Booking.Method.WALK_IN
            ):
                return detail(
                    'Only online-paid bookings can be refunded.',
                    status.HTTP_400_BAD_REQUEST,
                )
            if value == 'refunded' and booking.status == 'refunded':
                return Response(booking_row(booking))  # already refunded — no-op
            if value == 'refunded' and booking.razorpay_payment_id:
                # Real gateway refund (partial when refundAmount is sent).
                from bookings.razorpay_client import (
                    RazorpayError, configured, refund_payment,
                )
                if configured():
                    try:
                        booking.refund_id = refund_payment(
                            booking.razorpay_payment_id,
                            _to_int(data.get('refundAmount'), 0) or None,
                        )
                    except RazorpayError:
                        return detail(
                            'Refund failed at the payment gateway.',
                            status.HTTP_502_BAD_GATEWAY,
                        )
            booking.status = value
        # Refund details from the panel (stored alongside the status change).
        if 'reason' in data:
            booking.refund_reason = str(data['reason'] or '')[:200]
        if 'refundAmount' in data:
            booking.refund_amount = _to_int(data['refundAmount'], None)

        booking.save()
        record_audit(request, 'Booking update', booking.venue_name or booking.id,
                     booking.status, target_id=booking.id)
        return Response(booking_row(booking))


# ---------------------------------------------------------------
# New models (Phase 3) — settings, payouts, reviews, audit.
# ---------------------------------------------------------------

class AdminSettingsView(_AdminWriteView):
    """PUT /api/admin/settings — save the platform config; returns it."""

    def put(self, request):
        settings_obj = Settings.load()
        data = self._body(request)

        if 'fee' in data:
            settings_obj.booking_fee = _to_int(data['fee'], settings_obj.booking_fee)
        if 'feeDate' in data:
            settings_obj.fee_date = str(data['feeDate'] or '')
        for key in ('categories', 'cities', 'amenities', 'banners'):
            if isinstance(data.get(key), list):
                setattr(settings_obj, key, data[key])

        settings_obj.save()
        record_audit(request, 'Settings saved', 'settings', f'fee ₹{settings_obj.booking_fee}')
        return Response(settings_row(settings_obj))


class AdminPayoutUpdateView(_AdminWriteView):
    """PATCH /api/admin/payouts/<id> — process / retry (status)."""

    def patch(self, request, payout_id):
        payout = Payout.objects.filter(pk=payout_id).first()
        if payout is None:
            return detail('Payout not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        if 'status' in data:
            value = str(data['status'])
            if value not in PAYOUT_STATUSES:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
            payout.status = value

        payout.save()
        record_audit(request, 'Payout update', payout.vendor, payout.status, target_id=str(payout.pk))
        return Response(payout_row(payout))


class AdminReviewResolveView(_AdminWriteView):
    """POST /api/admin/reviews/<id>/resolve — keep or remove."""

    def post(self, request, review_id):
        review = Review.objects.filter(pk=review_id).first()
        if review is None:
            return detail('Review not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)

        action = str(data.get('action') or '')
        if action == 'keep':
            review.status = Review.Status.KEPT
        elif action == 'remove':
            review.status = Review.Status.REMOVED
            if data.get('reason'):
                review.reason = str(data['reason'])
        else:
            return detail('action must be "keep" or "remove".', status.HTTP_400_BAD_REQUEST)

        review.save()
        record_audit(request, f'Review {action}', review.venue, review.reason, target_id=str(review.pk))
        return Response(review_row(review))


class AdminAuditView(_AdminWriteView):
    """POST /api/admin/audit — append an audit row (panel-written)."""

    def post(self, request):
        data = self._body(request)
        entry = AuditEntry.objects.create(
            time=str(data.get('time') or ''),
            admin=str(data.get('admin') or ''),
            action=str(data.get('action') or ''),
            target=str(data.get('target') or ''),
            change=str(data.get('change') or ''),
        )
        return Response(audit_row(entry), status=status.HTTP_201_CREATED)
