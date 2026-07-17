"""
Serializers = translators between JSON (what the API speaks)
and Python objects (what Django speaks). They also validate input.
"""
from rest_framework import serializers

from .models import User, phone_validator


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


class OTPRequestSerializer(serializers.Serializer):
    """Input for POST .../auth/otp — just a 10-digit phone."""

    phone = serializers.CharField(validators=[phone_validator])


class OTPVerifySerializer(serializers.Serializer):
    """Input for POST .../auth/verify — phone + the 6-digit code."""

    phone = serializers.CharField(validators=[phone_validator])
    otp = serializers.RegexField(
        regex=r'^\d{6}$',
        error_messages={'invalid': 'OTP must be exactly 6 digits.'},
    )


class VendorRegisterSerializer(serializers.Serializer):
    """
    Input for POST /vendors — creates the vendor account AFTER the
    phone was OTP-verified (checked in the view, not here).
    """

    phone = serializers.CharField(validators=[phone_validator])
    name = serializers.CharField(max_length=150)  # required, non-empty
    email = serializers.EmailField(required=False, allow_blank=True, allow_null=True)
