"""
Super-admin editing of a vendor's profile — Vendors → (vendor) → Edit details.

    GET /api/admin/vendors/<id>/profile   the editable details
    PUT /api/admin/vendors/<id>/profile   save them

WHAT IS EDITABLE: the contact person's name and email (User), and the
business name / phone / email / address / city / state (VendorSettings —
the same row the vendor edits under their own Settings).

WHAT IS NOT:
  * Bank / payout details (PayoutDetails, the draft's payout section) are
    never read or returned here, and a request carrying any bank / payout /
    KYC-document key anywhere is refused (400 BANK_DETAILS_READ_ONLY).
  * The sign-in phone is the vendor's identity (OTP login) — read-only
    (400 SIGN_IN_PHONE_READ_ONLY). The business phone is the contact number.
  * Role, KYC, active state and venue ownership are not touched; KYC and
    suspension keep their own action (PATCH /api/admin/vendors/<id>).
  * Admin accounts are not edited from the vendors page (403).

Concurrency: GET returns `version` (a hash of the current values); PUT must
send it back, or 409 STALE_VERSION. Every save is audited with old → new.
"""
import hashlib
import json

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User, VendorSettings, phone_validator, vendor_accounts_q

from .auth import CsrfExemptSessionAuthentication, IsAdmin
from .venue_edit import find_private_keys

PROFILE_FIELDS = {'name': 150, 'email': 254}
# request key → (VendorSettings field, max length)
BUSINESS_FIELDS = {
    'name': ('business_name', 150),
    'phone': ('business_phone', 10),
    'email': ('business_email', 254),
    'address': ('business_address', 300),
    'city': ('business_city', 100),
    'state': ('business_state', 100),
}
LABELS = {
    'profile.name': 'contact name', 'profile.email': 'email',
    'business.name': 'business name', 'business.phone': 'business phone',
    'business.email': 'business email', 'business.address': 'business address',
    'business.city': 'city', 'business.state': 'state',
}
AUDIT_LIMIT = 250


class ProfileError(Exception):
    def __init__(self, message, code='INVALID', http_status=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.message, self.code, self.http_status = message, code, http_status


def _settings(vendor):
    return VendorSettings.objects.filter(user=vendor).first() or VendorSettings(user=vendor)


def values_of(vendor):
    row = _settings(vendor)
    return {
        'profile': {'name': vendor.name or '', 'email': vendor.email or ''},
        'business': {key: getattr(row, field) or '' for key, (field, _limit) in BUSINESS_FIELDS.items()},
    }


def version_of(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


def profile_payload(vendor):
    values = values_of(vendor)
    return {
        **values,
        'vendor': {
            'id': vendor.id,
            'phone': vendor.phone,          # sign-in identity — read-only
            'kyc': vendor.kyc,
            'acc': 'active' if vendor.is_active else 'suspended',
        },
        'version': version_of(values),
        'bankDetailsEditable': False,
    }


def _text(value, label, limit, required=False):
    text = str(value if value is not None else '').strip()
    if required and not text:
        raise ProfileError(f'Enter the {label}.')
    if len(text) > limit:
        raise ProfileError(f'The {label} can be at most {limit} characters.')
    return text


def _email(value, label):
    text = _text(value, label, 254)
    if text:
        try:
            validate_email(text)
        except ValidationError as error:
            raise ProfileError(f'Enter a valid {label}.') from error
    return text


def validate_profile(body):
    """{'profile': {...}, 'business': {...}} holding only the keys sent."""
    if not isinstance(body, dict):
        raise ProfileError('Request body must be an object.')
    if find_private_keys(body):
        raise ProfileError(
            'Bank and payout details cannot be viewed or edited here. They stay '
            'with the vendor’s payout details.',
            code='BANK_DETAILS_READ_ONLY',
        )
    unknown = set(body) - {'version', 'profile', 'business'}
    if unknown:
        raise ProfileError(f'Unknown field: {", ".join(sorted(unknown))}.')
    profile = body.get('profile') or {}
    business = body.get('business') or {}
    if not isinstance(profile, dict) or not isinstance(business, dict):
        raise ProfileError('profile and business must be objects.')
    if 'phone' in profile:
        raise ProfileError(
            'The sign-in phone number is the vendor’s login and can’t be changed '
            'here. Set the business phone for customer and booking contact.',
            code='SIGN_IN_PHONE_READ_ONLY',
        )
    bad = (set(profile) - set(PROFILE_FIELDS)) | {f'business.{k}' for k in set(business) - set(BUSINESS_FIELDS)}
    if bad:
        raise ProfileError(f'Unknown field: {", ".join(sorted(bad))}.')

    out = {'profile': {}, 'business': {}}
    if 'name' in profile:
        out['profile']['name'] = _text(profile['name'], 'contact name', 150, required=True)
    if 'email' in profile:
        out['profile']['email'] = _email(profile['email'], 'email')
    for key in business:
        _field, limit = BUSINESS_FIELDS[key]
        if key == 'email':
            out['business'][key] = _email(business[key], 'business email')
        elif key == 'phone':
            phone = _text(business[key], 'business phone', 10)
            if phone:
                try:
                    phone_validator(phone)
                except ValidationError as error:
                    raise ProfileError('Enter a 10-digit mobile number for the business phone.') from error
            out['business'][key] = phone
        else:
            out['business'][key] = _text(business[key], LABELS[f'business.{key}'], limit)
    return out


def apply_profile(vendor, body):
    """Validate and save; returns (payload, [(label, old, new), …])."""
    cleaned = validate_profile(body)
    with transaction.atomic():
        vendor = User.objects.select_for_update().get(pk=vendor.pk)
        before = values_of(vendor)
        if str(body.get('version') or '') != version_of(before):
            raise ProfileError(
                'These details were changed since you opened them. Reload and make '
                'your edit again.', code='STALE_VERSION', http_status=status.HTTP_409_CONFLICT,
            )
        profile = cleaned['profile']
        if 'email' in profile and profile['email'] and profile['email'].lower() != (vendor.email or '').lower():
            if User.objects.filter(email__iexact=profile['email']).exclude(pk=vendor.pk).exists():
                raise ProfileError(
                    'Another account already uses that email address.',
                    code='EMAIL_TAKEN', http_status=status.HTTP_409_CONFLICT,
                )
        if 'name' in profile:
            vendor.name = profile['name']
        if 'email' in profile:
            vendor.email = profile['email'] or None
        # Only these columns — never role, KYC, active state or password.
        vendor.save(update_fields=['name', 'email'])
        if cleaned['business']:
            row = _settings(vendor)
            for key, value in cleaned['business'].items():
                setattr(row, BUSINESS_FIELDS[key][0], value)
            row.save()
        after = values_of(vendor)
    changes = [
        (LABELS[f'{group}.{key}'], before[group][key], after[group][key])
        for group in ('profile', 'business') for key in after[group]
        if before[group][key] != after[group][key]
    ]
    return profile_payload(vendor), changes


def audit_change(changes):
    text = ' · '.join(f'{label}: {old or "—"} → {new or "—"}' for label, old, new in changes) or 'no changes'
    return text if len(text) <= AUDIT_LIMIT else text[:AUDIT_LIMIT - 1] + '…'


class AdminVendorProfileView(APIView):
    """GET / PUT /api/admin/vendors/<id>/profile — see the module docstring."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [JSONParser]

    def _vendor(self, vendor_id):
        vendor = User.objects.filter(vendor_accounts_q(), pk=vendor_id).first()
        if vendor is None:
            raise ProfileError('Vendor not found.', 'NOT_FOUND', status.HTTP_404_NOT_FOUND)
        if vendor.role == User.Role.ADMIN:
            raise ProfileError(
                'This account belongs to an admin and cannot be changed from the vendors page.',
                'ADMIN_ACCOUNT', status.HTTP_403_FORBIDDEN,
            )
        return vendor

    def get(self, request, vendor_id):
        try:
            vendor = self._vendor(vendor_id)
        except ProfileError as error:
            return Response({'detail': error.message, 'code': error.code}, status=error.http_status)
        return Response(profile_payload(vendor))

    def put(self, request, vendor_id):
        try:
            vendor = self._vendor(vendor_id)
            payload, changes = apply_profile(vendor, request.data)
        except ProfileError as error:
            return Response({'detail': error.message, 'code': error.code}, status=error.http_status)
        if changes:
            from .views import record_audit
            record_audit(
                request, 'Edited vendor details', payload['profile']['name'] or vendor.phone,
                audit_change(changes), target_id=str(vendor.pk),
            )
        return Response(payload)
