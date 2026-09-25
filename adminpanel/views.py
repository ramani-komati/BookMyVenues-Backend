"""
Super-admin panel endpoints (all under /api/admin/).

Auth is a 2-step COOKIE SESSION: password -> OTP (SMS via 2Factor) -> session.
Error shape everywhere: {"detail": "..."}.

Phase 1: auth + the aggregate `bootstrap` read. Writes and the new models
(payouts / reviews / audit / settings persistence) come in later phases.
"""
import datetime
import logging
import re
import uuid

from django.contrib.auth import login, logout
from django.db import transaction
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import PhoneOTP, User, vendor_accounts_q
from accounts.otp import OTPSendError, deliver_otp, generate_code
from bookings.models import Booking
from venues.models import Listing, VenueDraft
from venues.storage import ALLOWED_TYPES, StorageError, upload_photo
from venues.views import _looks_like_image as looks_like_image

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

logger = logging.getLogger(__name__)

REVIEW_STATUSES = {'pending', 'approved', 'changes', 'rejected'}
BOOKING_STATUSES = {'confirmed', 'completed', 'refund_pending', 'refunded', 'cancelled'}
PAYOUT_STATUSES = {'pending', 'failed', 'completed'}
# An admin-minted vendor token only has to outlive one registration wizard.
TOKEN_HOURS = 2


def _to_int(value, default=0):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _transition(before, after):
    """'pending → live' — computed from what the DB actually held, not from
    whatever the client believed the previous state was."""
    return f'{before} → {after}' if before != after else str(after)


def record_audit(request, action, target, change='', target_id='', reason=None,
                 subject=None):
    """Write THE audit row for an admin action — the server is the sole
    logger, so an action can never happen without being recorded (a
    client-sent entry would go missing whenever its fire-and-forget POST
    failed). `target` is the entity's NAME (stays readable even if the
    entity is later deleted); `target_id` its raw id. The timestamp always
    comes from the server clock (created_at)."""
    admin = getattr(request.user, 'name', '') or getattr(request.user, 'phone', '')
    reason = str(reason or '').strip()
    if reason:
        change = f'{change} · reason: {reason}' if change else f'reason: {reason}'
    # `subject` is the owner of the thing being acted on. When that is the
    # acting admin, say so — an admin approving/featuring their own venue is
    # a legitimate but self-interested action and should read as one.
    if subject is not None and getattr(subject, 'id', None) == request.user.id:
        change = f'{change} · SELF-ACTION' if change else 'SELF-ACTION'
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
    return User.objects.filter(
        email__iexact=email, role=User.Role.ADMIN, is_active=True
    ).first()


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
        # wrong — and the same amount of WORK either way, so response timing
        # cannot be used to discover which admin emails exist.
        if admin is None:
            check_password(password, make_password('timing-equaliser'))
            return detail('Invalid email or password.', status.HTTP_400_BAD_REQUEST)
        if not admin.check_password(password):
            return detail('Invalid email or password.', status.HTTP_400_BAD_REQUEST)

        code = generate_code()
        # Admins sign in WITH their email, so both channels always apply.
        try:
            deliver_otp(code, admin.phone, admin.email)
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

        # Establish the session cookie. NOTE: `token` is deliberately not the
        # session key — auth is the HttpOnly cookie, and echoing the key into a
        # readable body would hand it to anything that can see the response.
        login(request, admin, backend=_BACKEND)
        return Response({'token': 'session'})


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
    # JSON ONLY — and that is a security control, not a preference. Admin
    # writes are CSRF-exempt (decoupled SPA) and the session cookie is
    # SameSite=None, so protection rests entirely on the CORS origin
    # allowlist. Form-encoded and multipart POSTs are CORS-"simple": the
    # browser sends them cross-site with cookies and NEVER preflights, so the
    # allowlist would never be consulted and a plain <form> on any website
    # could drive this API as a logged-in admin. Requiring JSON forces a
    # preflight on every admin write.
    parser_classes = [JSONParser]

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
        previous = listing.status

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
        if listing.status != previous:
            actions = {
                Listing.Status.LIVE: 'Approved venue',
                Listing.Status.REJECTED: 'Rejected venue',
                Listing.Status.CHANGES: 'Requested changes',
                Listing.Status.PENDING: 'Reopened approval',
            }
            record_audit(
                request, actions.get(listing.status, 'Updated approval'),
                listing.name, _transition(previous, listing.status),
                target_id=str(listing.pk), reason=data.get('reason'),
            )
        elif 'checks' in data or 'notes' in data:
            record_audit(
                request, 'Updated review notes' if 'notes' in data
                else 'Updated review checklist',
                listing.name, '', target_id=str(listing.pk),
            )
        return Response(approval_row(listing))


def _clean_part_payment(raw):
    """(config_or_None, error). None means "turn the feature off"."""
    from bookings.part_payment import FIXED, MAX_PERCENT, MIN_PERCENT, PERCENT

    if raw in (None, {}, False):
        return None, None
    if not isinstance(raw, dict):
        return None, 'partPayment must be an object.'
    if not raw.get('enabled'):
        return None, None

    mode = str(raw.get('mode') or '').strip().lower()
    if mode not in (PERCENT, FIXED):
        return None, 'partPayment.mode must be "percent" or "fixed".'
    try:
        value = int(float(str(raw.get('value'))))
    except (TypeError, ValueError):
        return None, 'partPayment.value must be a number.'

    if mode == PERCENT and not (MIN_PERCENT <= value <= MAX_PERCENT):
        return None, (
            f'partPayment.value must be between {MIN_PERCENT} and '
            f'{MAX_PERCENT} percent.'
        )
    if mode == FIXED and value <= 0:
        return None, 'partPayment.value must be more than ₹0.'

    return {'enabled': True, 'mode': mode, 'value': value}, None


class AdminVenueUpdateView(_AdminWriteView):
    """PATCH /api/admin/venues/<id> — status (live/paused) / featured.

    Status changes apply to the venue's WHOLE listing set: the base row and
    its unit siblings (detail.unitOf family) move together, in both
    directions — pause hides all, live restores all."""

    def patch(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id).first()
        if listing is None:
            return detail('Venue not found.', status.HTTP_404_NOT_FOUND)
        if listing.status == Listing.Status.DELETED:
            return detail(
                'This venue is already deleted and can no longer be changed.',
                status.HTTP_409_CONFLICT,
            )
        data = self._body(request)
        previous, was_featured = listing.status, listing.featured
        was_requested = listing.deletion_requested_at is not None

        if 'status' in data:
            value = str(data['status'])
            if value == 'deleted':
                # APPROVE a vendor's deletion request — the real delete.
                # This endpoint only ever APPROVES: with no pending request
                # there is nothing to approve, and allowing it would turn the
                # vendor-consent flow into a unilateral admin delete.
                if not was_requested:
                    return detail(
                        'This venue has not requested deletion. Ask the '
                        'vendor to request it first.',
                        status.HTTP_409_CONFLICT,
                    )
                from venues.views import has_upcoming_bookings, soft_delete_listing
                if has_upcoming_bookings(listing):
                    return detail(
                        'This venue has upcoming bookings. They must be '
                        'cancelled or refunded before it can be deleted.',
                        status.HTTP_409_CONFLICT,
                    )
                soft_delete_listing(listing)
                record_audit(
                    request, 'Approved venue deletion', listing.name,
                    _transition(previous, 'deleted'),
                    target_id=str(listing.pk), reason=data.get('reason'),
                    subject=listing.vendor,
                )
                return Response(venue_row(listing))
            if value == 'live' and was_requested:
                # REJECT the request: keep whatever status it already had
                # (live or paused) and just clear the pending flag.
                listing.deletion_requested_at = None
                listing.save(update_fields=['deletion_requested_at'])
                record_audit(
                    request, 'Rejected venue deletion', listing.name,
                    f'stays {listing.status}', target_id=str(listing.pk),
                    reason=data.get('reason'),
                )
                return Response(venue_row(listing))
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

        if 'partPayment' in data:
            # This decides how much money we actually collect online, so it is
            # validated rather than stored verbatim like the vendor's own
            # display fields. A bad shape is refused instead of silently
            # disabling the split and over-charging the customer.
            config, error = _clean_part_payment(data['partPayment'])
            if error:
                return detail(error, status.HTTP_400_BAD_REQUEST)
            record = dict(listing.record or {})
            if config is None:
                record.pop('partPayment', None)
            else:
                record['partPayment'] = config
            listing.record = record
            record_audit(
                request, 'Updated part payment', listing.name,
                'disabled' if config is None
                else f"{config['mode']} {config['value']}",
                target_id=str(listing.pk), subject=listing.vendor,
            )

        listing.save()
        if 'partPayment' in data:
            # The public venue detail is cached; without this the customer app
            # keeps quoting the old split.
            from venues.public_views import invalidate_listing_cache
            invalidate_listing_cache(listing)
        if listing.status != previous:
            record_audit(
                request,
                'Paused venue' if listing.status == Listing.Status.PAUSED
                else 'Unpaused venue',
                listing.name, _transition(previous, listing.status),
                target_id=str(listing.pk), reason=data.get('reason'),
            )
        if listing.featured != was_featured:
            record_audit(
                request,
                'Featured venue' if listing.featured else 'Unfeatured venue',
                listing.name,
                f"homepage feature: {'on' if listing.featured else 'off'}",
                target_id=str(listing.pk),
            )
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


class AdminVendorTokenView(_AdminWriteView):
    """
    POST /api/admin/vendors/token {phone, name?} -> a vendor JWT.

    Lets an admin register a venue on an owner's behalf by reusing the normal
    vendor pipeline (/venues/drafts/* -> /submit) instead of duplicating it.
    Creates the vendor if that phone is new — the admin session is the
    authorization, so no OTP.

    SECURITY — this is admin impersonation of a vendor, and the token it
    returns is a FULL vendor token: it also opens /vendors/me/dashboard
    (earnings), payout details and venue deletion, not just the draft
    endpoints. Three things narrow that:
      * it expires in 2 hours, not the usual 30 days,
      * it carries an `actor` claim naming the admin who minted it, and
      * every mint is written to the audit log.
    Admin accounts are refused outright — one admin must not be able to mint
    a token for another.
    """

    def post(self, request):
        data = self._body(request)
        phone = ''.join(ch for ch in str(data.get('phone') or '') if ch.isdigit())
        if len(phone) == 11 and phone.startswith('0'):
            phone = phone[1:]
        if len(phone) == 12 and phone.startswith('91'):
            phone = phone[2:]
        if not re.fullmatch(r'[6-9]\d{9}', phone):
            return detail(
                'A valid 10-digit mobile number is required.',
                status.HTTP_400_BAD_REQUEST,
            )

        name = str(data.get('name') or '').strip()
        vendor = User.objects.filter(phone=phone).first()
        created = vendor is None

        if created:
            vendor = User.objects.create_user(
                phone=phone, name=name, role=User.Role.VENDOR,
            )
            vendor.is_vendor = True
            vendor.save(update_fields=['is_vendor'])
        else:
            if vendor.role == User.Role.ADMIN:
                return detail(
                    'That number belongs to an admin account.',
                    status.HTTP_403_FORBIDDEN,
                )
            if not vendor.is_active:
                return detail(
                    'That vendor account is blocked.',
                    status.HTTP_403_FORBIDDEN,
                )
            fields = []
            # Only ever FILL a blank name — never overwrite what the vendor set.
            if name and not vendor.name:
                vendor.name = name
                fields.append('name')
            if not vendor.is_vendor:
                vendor.is_vendor = True
                fields.append('is_vendor')
            if fields:
                vendor.save(update_fields=fields)

        token = RefreshToken.for_user(vendor).access_token
        token.set_exp(lifetime=datetime.timedelta(hours=TOKEN_HOURS))
        # Traceable: the token still authenticates AS the vendor, but records
        # who minted it.
        token['actor'] = f'admin:{request.user.id}'

        record_audit(
            request,
            'Created vendor' if created else 'Issued vendor token',
            vendor.name or vendor.phone,
            f'vendor token issued ({TOKEN_HOURS}h)',
            target_id=str(vendor.id),
            subject=vendor,
        )

        return Response({
            'vendor': {
                'id': str(vendor.id), 'phone': vendor.phone,
                'name': vendor.name or '', 'email': vendor.email or '',
            },
            'token': str(token),
            'created': created,
        })


class AdminVendorUpdateView(_AdminWriteView):
    """PATCH /api/admin/vendors/<id> — kyc / acc (suspend/reactivate).

    Suspending removes the vendor from the platform: refused (409) while any
    of their venues has an active booking; otherwise all their live venues
    (incl. unit siblings) are paused and the account is deactivated — which
    also blocks re-publishing, since inactive accounts fail authentication.
    Reactivating does NOT auto-relist: venues stay paused for manual review."""

    def patch(self, request, vendor_id):
        vendor = User.objects.filter(vendor_accounts_q(), pk=vendor_id).first()
        if vendor is None:
            return detail('Vendor not found.', status.HTTP_404_NOT_FOUND)
        # Admins hold is_vendor too, so they match the vendor lookup. Suspending
        # one here would flip is_active and lock them out of the admin panel
        # entirely — that is not a vendor-moderation action.
        if vendor.role == User.Role.ADMIN:
            return detail(
                'This account belongs to an admin and cannot be changed '
                'from the vendors page.',
                status.HTTP_403_FORBIDDEN,
            )
        data = self._body(request)
        was_kyc, was_active = vendor.kyc, vendor.is_active

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
        label = vendor.name or vendor.phone
        if vendor.kyc != was_kyc:
            record_audit(
                request, f'KYC {vendor.kyc}', label,
                _transition(was_kyc, vendor.kyc), target_id=str(vendor.pk),
                reason=data.get('reason'),
            )
        if vendor.is_active != was_active:
            record_audit(
                request,
                'Reactivated vendor' if vendor.is_active else 'Suspended vendor',
                label, _transition(
                    'active' if was_active else 'suspended',
                    'active' if vendor.is_active else 'suspended',
                ),
                target_id=str(vendor.pk), reason=data.get('reason'),
            )
        return Response(vendor_row(vendor))


class AdminUserUpdateView(_AdminWriteView):
    """PATCH /api/admin/users/<id> — status (block/unblock)."""

    def patch(self, request, user_id):
        user = User.objects.filter(pk=user_id, role=User.Role.PUBLIC).first()
        if user is None:
            return detail('User not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)
        was_active = user.is_active

        if 'status' in data:
            value = str(data['status'])
            if value == 'blocked':
                user.is_active = False
            elif value == 'active':
                user.is_active = True
            else:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)

        user.save()
        if user.is_active != was_active:
            record_audit(
                request, 'Unblocked user' if user.is_active else 'Blocked user',
                user.name or user.phone,
                _transition(
                    'active' if was_active else 'blocked',
                    'active' if user.is_active else 'blocked',
                ),
                target_id=str(user.pk), reason=data.get('reason'),
            )
        return Response(user_row(user))


class AdminBookingUpdateView(_AdminWriteView):
    """PATCH /api/admin/bookings/<id> — status (e.g. refunded)."""

    def patch(self, request, booking_id):
        # Lock the row for the whole read-check-refund-write sequence: two
        # concurrent PATCHes (double-click, two tabs) could otherwise both see
        # "not yet refunded" and fire two real gateway refunds.
        with transaction.atomic():
            return self._patch_locked(request, booking_id)

    def _patch_locked(self, request, booking_id):
        booking = (
            Booking.objects.select_for_update().filter(pk=booking_id).first()
        )
        if booking is None:
            return detail('Booking not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)
        previous_status = booking.status

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
                refund_amount = _to_int(data.get('refundAmount'), 0) or None
                # Cap at what was CAPTURED, not the booking total: a part-paid
                # booking only ever charged its online slice, and the cash the
                # vendor took at the venue is not ours to refund.
                refundable = booking.online_amount
                if refund_amount is not None and not (
                    0 < refund_amount <= refundable
                ):
                    return detail(
                        f'refundAmount must be between 1 and {refundable}.',
                        status.HTTP_400_BAD_REQUEST,
                    )
                if configured():
                    try:
                        booking.refund_id = refund_payment(
                            booking.razorpay_payment_id, refund_amount,
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
        if booking.status != previous_status:
            amount = booking.refund_amount or booking.amount
            record_audit(
                request,
                'Issued refund' if booking.status == 'refunded' else 'Updated booking',
                f'#{booking.id} · ₹{amount}' if booking.status == 'refunded'
                else (booking.venue_name or booking.id),
                _transition(previous_status, booking.status),
                target_id=booking.id, reason=booking.refund_reason or data.get('reason'),
            )
        return Response(booking_row(booking))


# ---------------------------------------------------------------
# New models (Phase 3) — settings, payouts, reviews, audit.
# ---------------------------------------------------------------

# Schemes a banner image may use. The banners list is stored verbatim and
# served to every visitor, so the one field that becomes a src on the public
# homepage is checked — a javascript:/data: URL has no business there.
_SAFE_IMAGE_PREFIXES = ('http://', 'https://', '/')


def _check_banner_images(banners):
    """Error string for an unusable banner image, else None."""
    for banner in banners:
        if not isinstance(banner, dict):
            continue
        image = str(banner.get('image') or '').strip()
        if not image:
            continue                    # optional — absent or blank is fine
        if not image.lower().startswith(_SAFE_IMAGE_PREFIXES):
            title = str(banner.get('title') or 'banner')
            return (
                f'"{title}" has an unusable image URL — it must start with '
                f'https://, http:// or /.'
            )
    return None


class AdminUploadView(APIView):
    """
    POST /api/admin/uploads — multipart image in, hosted URL out.

        {"url": "https://<project>.supabase.co/storage/v1/object/public/..."}

    Stored in the same Supabase bucket as venue photos, so the returned URL is
    permanent and already https — which satisfies the banner image guard.

    SECURITY, and why the X-Requested-With header is required: every other
    admin write is JSON-only precisely BECAUSE admin writes are CSRF-exempt
    with a SameSite=None cookie, and a multipart POST is CORS-"simple" — the
    browser sends it cross-site with the admin's cookie and never preflights,
    so the origin allowlist is never consulted. An upload cannot be JSON, so
    it asks for a custom header instead: any custom header makes the request
    non-simple, which forces the preflight back and restores the allowlist.
    Without it, any website an admin visited could push files into our bucket.
    """

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [MultiPartParser, FormParser]

    MAX_BYTES = 6 * 1024 * 1024        # ~6 MB

    def post(self, request):
        if not request.headers.get('X-Requested-With'):
            return detail(
                'Uploads must send an X-Requested-With header.',
                status.HTTP_400_BAD_REQUEST,
            )

        upload = request.FILES.get('image') or request.FILES.get('file')
        if upload is None:
            return detail('No file uploaded.', status.HTTP_400_BAD_REQUEST)

        extension = ALLOWED_TYPES.get(upload.content_type)
        if extension is None:
            return detail(
                'Only JPEG, PNG or WebP images are allowed.',
                status.HTTP_400_BAD_REQUEST,
            )
        if upload.size > self.MAX_BYTES:
            return detail(
                f'Image is too large (max {self.MAX_BYTES // (1024 * 1024)} MB).',
                status.HTTP_400_BAD_REQUEST,
            )
        # content_type is a client-supplied label; the bytes decide.
        if not looks_like_image(upload):
            return detail(
                'That file is not a valid JPEG, PNG or WebP image.',
                status.HTTP_400_BAD_REQUEST,
            )

        path = f'admin/{uuid.uuid4().hex}.{extension}'
        try:
            url = upload_photo(path, upload.read(), upload.content_type)
        except StorageError as error:
            logger.warning('Admin upload failed: %s', error)
            return detail(
                'Could not store the image right now. Please try again.',
                status.HTTP_502_BAD_GATEWAY,
            )

        record_audit(request, 'Uploaded an image', upload.name or 'image',
                     f'{upload.size // 1024} KB')
        return Response({'url': url}, status=status.HTTP_201_CREATED)


class AdminSettingsView(_AdminWriteView):
    """PUT /api/admin/settings — save the platform config; returns it."""

    def put(self, request):
        settings_obj = Settings.load()
        data = self._body(request)

        if 'fee' in data:
            settings_obj.booking_fee = _to_int(data['fee'], settings_obj.booking_fee)
        if 'feeDate' in data:
            settings_obj.fee_date = str(data['feeDate'] or '')
        if isinstance(data.get('banners'), list):
            error = _check_banner_images(data['banners'])
            if error:
                return detail(error, status.HTTP_400_BAD_REQUEST)

        for key in ('categories', 'cities', 'amenities', 'banners'):
            if isinstance(data.get(key), list):
                setattr(settings_obj, key, data[key])

        settings_obj.save()
        record_audit(
            request, 'Updated platform settings', 'Fees, categories, content',
            f'fee ₹{settings_obj.booking_fee}'
            + (f' eff. {settings_obj.fee_date}' if settings_obj.fee_date else ''),
        )
        return Response(settings_row(settings_obj))


class AdminPayoutUpdateView(_AdminWriteView):
    """PATCH /api/admin/payouts/<id> — process / retry (status)."""

    def patch(self, request, payout_id):
        payout = Payout.objects.filter(pk=payout_id).first()
        if payout is None:
            return detail('Payout not found.', status.HTTP_404_NOT_FOUND)
        data = self._body(request)
        previous_status = payout.status

        if 'status' in data:
            value = str(data['status'])
            if value not in PAYOUT_STATUSES:
                return detail('Invalid status.', status.HTTP_400_BAD_REQUEST)
            payout.status = value

        payout.save()
        record_audit(
            request, f'Payout {payout.status}', payout.vendor,
            _transition(previous_status, payout.status), target_id=str(payout.pk),
        )
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
        record_audit(
            request, 'Removed review' if action == 'remove' else 'Kept review',
            review.venue, '', target_id=str(review.pk), reason=review.reason,
        )
        return Response(review_row(review))


class AdminAuditView(_AdminWriteView):
    """POST /api/admin/audit — legacy panel-written entry.

    The server now logs every mutation itself, so this endpoint DEDUPES: if
    an entry for the same target was written in the last few seconds (i.e.
    the server already recorded this action), the panel's copy is dropped and
    the existing row is returned. That kills the double-logging immediately,
    without waiting for the panel to stop calling it. Entries for actions the
    server can't see (e.g. 'Logged in') are still recorded.

    The client's `time` is ignored — timestamps come from the server clock.
    """

    DEDUPE_SECONDS = 15

    def post(self, request):
        data = self._body(request)
        target = str(data.get('target') or '')
        # `admin` is NEVER taken from the body — an audit trail that lets one
        # admin attribute an action to another proves nothing.
        acting = getattr(request.user, 'name', '') or getattr(request.user, 'phone', '')

        recent = AuditEntry.objects.filter(
            target=target,
            created_at__gte=timezone.now() - datetime.timedelta(seconds=self.DEDUPE_SECONDS),
        ).first()
        if recent is not None:
            return Response(audit_row(recent), status=status.HTTP_200_OK)

        entry = AuditEntry.objects.create(
            admin=acting,
            action=str(data.get('action') or ''),
            target=target,
            change=str(data.get('change') or ''),
        )
        return Response(audit_row(entry), status=status.HTTP_201_CREATED)
