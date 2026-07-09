"""
Serializers = translators between JSON (what the API speaks)
and Python objects (what Django speaks). They also validate input.
"""
from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    """Safe, public view of a user — matches the API contract.
    Note: password is NOT here, so it can never leak into a response."""

    class Meta:
        model = User
        fields = ['id', 'name', 'phone', 'role']


class VendorRegisterSerializer(serializers.ModelSerializer):
    """
    Validates registration input {name, phone, email, password}.

    Free validation we inherit from the model automatically:
    - phone: must be 10 digits (phone_validator) + must be unique
    - email: must be a valid address + must be unique
    Each failure becomes a field-level error, e.g.
    {"phone": ["Phone number must be exactly 10 digits."]}
    """

    # write_only: accepted as input but never included in responses.
    password = serializers.CharField(
        write_only=True,
        min_length=8,
        error_messages={'min_length': 'Password must be at least 8 characters.'},
    )

    class Meta:
        model = User
        fields = ['name', 'phone', 'email', 'password']

    def create(self, validated_data):
        # create_user() hashes the password; role is forced to VENDOR here —
        # a client can never sneak in role=ADMIN via this endpoint.
        return User.objects.create_user(role=User.Role.VENDOR, **validated_data)
