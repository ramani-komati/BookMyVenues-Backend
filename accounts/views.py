"""
Auth views — phone + OTP login for customers and vendors.

Flow (customer):   POST /users/auth/otp -> SMS -> POST /users/auth/verify -> JWT
Flow (vendor, returning): same via /vendors/... -> JWT
Flow (vendor, new): verify returns {isNew: true} -> POST /vendors {name,email} -> JWT

All OTP validation happens HERE on the server. 2Factor only delivers SMS.
Error shape everywhere: {"message": "..."} (frontend contract).
"""
import datetime

from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import PhoneOTP, User, vendor_accounts_q
from .otp import OTPSendError, generate_code, send_otp_sms
from .serializers import (
    OTPRequestSerializer,
    OTPVerifySerializer,
    ProfileSerializer,
    UserSerializer,
    VendorRegisterSerializer,
)

# Max OTP SMS per phone number per 10 minutes (protects the SMS balance
# and stops attackers from bombarding someone's phone).
OTP_REQUESTS_PER_WINDOW = 3
OTP_REQUEST_WINDOW = datetime.timedelta(minutes=10)


def _message(text, http_status):
    """Shortcut for the contract's error shape."""
    return Response({'message': text}, status=http_status)


def _first_error(errors):
    """Flatten DRF's {"field": ["msg", ...]} into one human sentence."""
    for messages in errors.values():
        first = messages[0] if isinstance(messages, (list, tuple)) else messages
        return str(first)
    return 'Invalid input.'


def _access_token(user):
    """Contract responses carry a single "token" (JWT access, 30 days)."""
    return str(RefreshToken.for_user(user).access_token)


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """GET /api/v1/health — lets the frontend/Render check the API is alive."""
    return Response({'status': 'ok'})


class MeView(APIView):
    """GET /api/v1/auth/me — who am I? (requires Bearer token)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


# ---------------------------------------------------------------
# Profile update (customers and vendors) — contract P4
# ---------------------------------------------------------------

def _apply_profile_update(user, data):
    """
    Apply {name?, email?} to the user IN PLACE and return an error message,
    or None on success. `phone` is NEVER read from the body — it is the
    sign-in identity and must stay immutable.
    """
    if not isinstance(data, dict):
        return 'Request body must be a JSON object.'

    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if not name:
            return 'Name cannot be empty.'
        user.name = name

    if 'email' in data:
        email = str(data.get('email') or '').strip()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                return 'Enter a valid email address.'
            if User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists():
                return 'Email already in use.'
            user.email = email
        else:
            user.email = None  # clearing the email is allowed
    return None


class UserProfileUpdateView(APIView):
    """PATCH /api/users/me {name?, email?} -> {"user": {phone, name, email}}."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        error = _apply_profile_update(request.user, request.data)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)
        request.user.save()
        return Response({'user': ProfileSerializer(request.user).data})


class VendorProfileUpdateView(APIView):
    """PATCH /api/vendors/me {name?, email?} -> {"vendor": {phone, name, email}}."""

    permission_classes = [IsAuthenticated]

    def patch(self, request):
        if not request.user.has_vendor_access:
            return _message(
                'Only vendor accounts can access this endpoint.',
                status.HTTP_403_FORBIDDEN,
            )
        error = _apply_profile_update(request.user, request.data)
        if error:
            return _message(error, status.HTTP_400_BAD_REQUEST)
        request.user.save()
        return Response({'vendor': ProfileSerializer(request.user).data})


# ---------------------------------------------------------------
# OTP request (shared by customers and vendors)
# ---------------------------------------------------------------

class BaseOTPRequestView(APIView):
    """POST {phone} -> generate + SMS a 6-digit OTP -> {"sentTo": phone}."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'
    purpose = None  # set by subclasses

    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return _message(_first_error(serializer.errors), status.HTTP_400_BAD_REQUEST)

        phone = serializer.validated_data['phone']

        # Blocked accounts cannot even request an OTP.
        blocked = User.objects.filter(phone=phone, is_active=False).exists()
        if blocked:
            return _message('This account has been blocked.', status.HTTP_403_FORBIDDEN)

        # Per-phone limit, on top of the per-IP throttle.
        window_start = timezone.now() - OTP_REQUEST_WINDOW
        recent = PhoneOTP.objects.filter(phone=phone, created_at__gte=window_start).count()
        if recent >= OTP_REQUESTS_PER_WINDOW:
            return _message(
                'Too many OTP requests. Please try again in a few minutes.',
                status.HTTP_429_TOO_MANY_REQUESTS,
            )

        code = generate_code()
        try:
            send_otp_sms(phone, code)
        except OTPSendError:
            # Never store an OTP the user did not receive.
            return _message(
                'Could not send the OTP right now. Please try again.',
                status.HTTP_502_BAD_GATEWAY,
            )

        # A new OTP invalidates all previous ones for this phone+purpose.
        PhoneOTP.objects.filter(phone=phone, purpose=self.purpose, used=False).update(used=True)
        PhoneOTP.objects.create(
            phone=phone,
            purpose=self.purpose,
            code_hash=make_password(code),  # hashed — never stored in plain text
            expires_at=timezone.now() + datetime.timedelta(minutes=PhoneOTP.LIFETIME_MINUTES),
        )
        return Response({'sentTo': phone})


class UserOTPRequestView(BaseOTPRequestView):
    """POST /api/users/auth/otp"""
    purpose = PhoneOTP.Purpose.USER


class VendorOTPRequestView(BaseOTPRequestView):
    """POST /api/vendors/auth/otp"""
    purpose = PhoneOTP.Purpose.VENDOR


# ---------------------------------------------------------------
# OTP verify
# ---------------------------------------------------------------

def _check_otp(phone, code, purpose):
    """
    Validate a submitted code. Returns (otp_record, None) on success
    or (None, error_response) on failure.
    """
    otp = PhoneOTP.objects.filter(phone=phone, purpose=purpose, used=False).first()

    if otp is None or timezone.now() >= otp.expires_at:
        return None, _message(
            'OTP expired or not requested. Please request a new one.',
            status.HTTP_401_UNAUTHORIZED,
        )

    if otp.attempts >= PhoneOTP.MAX_ATTEMPTS:
        return None, _message(
            'Too many wrong attempts. Please request a new OTP.',
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    if not check_password(code, otp.code_hash):
        otp.attempts += 1
        otp.save(update_fields=['attempts'])
        return None, _message('Incorrect OTP.', status.HTTP_401_UNAUTHORIZED)

    return otp, None


class UserOTPVerifyView(APIView):
    """
    POST /api/users/auth/verify {phone, otp}
    -> {"user": {...}, "token": "jwt"} — creates the customer if new.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return _message(_first_error(serializer.errors), status.HTTP_400_BAD_REQUEST)

        phone = serializer.validated_data['phone']

        # Blocked accounts cannot sign in (enforced at verify too, in case an
        # OTP was issued before the block landed).
        if User.objects.filter(phone=phone, is_active=False).exists():
            return _message('This account has been blocked.', status.HTTP_403_FORBIDDEN)

        otp, error = _check_otp(phone, serializer.validated_data['otp'], PhoneOTP.Purpose.USER)
        if error:
            return error

        # Consume the OTP — single use.
        otp.used = True
        otp.verified = True
        otp.save(update_fields=['used', 'verified'])

        # First-time customers get an account automatically (no signup form).
        user, _ = User.objects.get_or_create(
            phone=phone, defaults={'is_customer': True}
        )
        # A vendor logging in through the customer app is ALSO a customer.
        if not user.is_customer:
            user.is_customer = True
            user.save(update_fields=['is_customer'])

        return Response({
            'user': ProfileSerializer(user).data,
            'token': _access_token(user),
        })


class VendorOTPVerifyView(APIView):
    """
    POST /api/vendors/auth/verify {phone, otp}
    Returning vendor -> {"vendor": {...}, "isNew": false, "token": "jwt"}
    Unknown phone    -> {"vendor": null,  "isNew": true}   (no token yet —
    the frontend shows a signup form and then calls POST /vendors).
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return _message(_first_error(serializer.errors), status.HTTP_400_BAD_REQUEST)

        phone = serializer.validated_data['phone']

        if User.objects.filter(phone=phone, is_active=False).exists():
            return _message('This account has been blocked.', status.HTTP_403_FORBIDDEN)

        otp, error = _check_otp(phone, serializer.validated_data['otp'], PhoneOTP.Purpose.VENDOR)
        if error:
            return error

        vendor = User.objects.filter(vendor_accounts_q(), phone=phone).first()

        if vendor is not None:
            otp.used = True
            otp.verified = True
            otp.save(update_fields=['used', 'verified'])
            return Response({
                'vendor': ProfileSerializer(vendor).data,
                'isNew': False,
                'token': _access_token(vendor),
            })

        # New vendor: mark verified but NOT used — POST /vendors will
        # consume it. This proves to the register endpoint that this
        # phone really passed OTP moments ago.
        otp.verified = True
        otp.save(update_fields=['verified'])
        return Response({'vendor': None, 'isNew': True})


class VendorRegisterView(APIView):
    """
    POST /api/vendors {phone, name, email}
    -> {"vendor": {...}, "token": "jwt"} (201)

    Only works if the phone has a verified (and still unconsumed)
    vendor OTP from the last 30 minutes.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = VendorRegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return _message(_first_error(serializer.errors), status.HTTP_400_BAD_REQUEST)

        phone = serializer.validated_data['phone']
        name = serializer.validated_data['name']
        email = serializer.validated_data.get('email') or None

        # SECURITY: registration must follow a real OTP verification.
        window_start = timezone.now() - datetime.timedelta(
            minutes=PhoneOTP.REGISTER_WINDOW_MINUTES
        )
        otp = PhoneOTP.objects.filter(
            phone=phone,
            purpose=PhoneOTP.Purpose.VENDOR,
            verified=True,
            used=False,
            created_at__gte=window_start,
        ).first()
        if otp is None:
            return _message(
                'Phone not verified. Please verify with OTP first.',
                status.HTTP_403_FORBIDDEN,
            )

        if User.objects.filter(vendor_accounts_q(), phone=phone).exists():
            return _message('Phone already registered as a vendor.', status.HTTP_409_CONFLICT)

        if email and User.objects.filter(email=email).exclude(phone=phone).exists():
            return _message('Email already in use.', status.HTTP_400_BAD_REQUEST)

        existing = User.objects.filter(phone=phone).first()
        if existing is not None:
            # Phone already has a customer account — upgrade it to vendor.
            # Never demote an ADMIN to VENDOR — the flag carries vendor access.
            if existing.role != User.Role.ADMIN:
                existing.role = User.Role.VENDOR
            existing.is_vendor = True
            existing.name = name
            existing.email = email
            existing.save(update_fields=['role', 'is_vendor', 'name', 'email'])
            vendor = existing
        else:
            vendor = User.objects.create_user(
                phone=phone, name=name, email=email,
                role=User.Role.VENDOR, is_vendor=True,
            )

        otp.used = True
        otp.save(update_fields=['used'])

        return Response(
            {'vendor': ProfileSerializer(vendor).data, 'token': _access_token(vendor)},
            status=status.HTTP_201_CREATED,
        )
