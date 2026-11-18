"""
Serializers = translators between JSON (what the API speaks)
and Python objects (what Django speaks). They also validate input.
"""
import re

from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """Full safe view of a user (used by /auth/me). No password/OTP here."""

    class Meta:
        model = User
        fields = ['id', 'name', 'phone', 'email', 'role']


class ProfileSerializer(serializers.ModelSerializer):
    """Exact {phone, name, email} shape the frontend contract uses for
    the "user" and "vendor" objects in auth responses."""

    class Meta:
        model = User
        fields = ['phone', 'name', 'email']


class MobileNumberField(serializers.CharField):
    """An Indian MOBILE number for sign-in, normalised to 10 digits.

    Accepts what people type — spaces, hyphens, brackets, a +91 / 91 / 0
    prefix — and stores the bare 10 digits. Refuses letters (QA audit
    2026-10-30: "12345abc12345" was reduced to digits and an OTP was sent to
    a number nobody typed) and anything that is not a mobile number (must
    start with 6, 7, 8 or 9), so no OTP is ever requested for it."""

    default_error_messages = {
        'mobile': 'Enter a valid 10-digit Indian mobile number (starting with 6, 7, 8 or 9).',
    }

    def to_internal_value(self, data):
        text = super().to_internal_value(data)
        number = normalize_mobile(text)
        if number is None:
            self.fail('mobile')
        return number


def normalize_mobile(value):
    """'+91 98765-43210' -> '9876543210'; None for anything that isn't one."""
    text = str(value or '').strip()
    if not text or not re.fullmatch(r'\+?[\d\s().-]+', text):
        return None   # letters or other symbols: never "fixed" by stripping
    digits = re.sub(r'\D', '', text)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    return digits if re.fullmatch(r'[6-9]\d{9}', digits) else None


class OTPRequestSerializer(serializers.Serializer):
    """Input for POST .../auth/otp — just a 10-digit mobile number."""

    phone = MobileNumberField()


class OTPVerifySerializer(serializers.Serializer):
    """Input for POST .../auth/verify — phone + the 6-digit code."""

    phone = MobileNumberField()
    otp = serializers.RegexField(
        regex=r'^\d{6}$',
        error_messages={'invalid': 'OTP must be exactly 6 digits.'},
    )


class VendorRegisterSerializer(serializers.Serializer):
    """
    Input for POST /vendors — creates the vendor account AFTER the
    phone was OTP-verified (checked in the view, not here).
    """

    phone = MobileNumberField()
    name = serializers.CharField(max_length=150)  # required, non-empty
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
