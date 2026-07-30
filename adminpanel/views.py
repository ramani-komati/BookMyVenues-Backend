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

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from .formatters import build_bootstrap

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
