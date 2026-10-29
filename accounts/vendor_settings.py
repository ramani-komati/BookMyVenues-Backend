"""
Vendor settings — business contact details, alert preferences and a
read-only view of the account's standing.

    GET   /api/vendors/me/settings
    PATCH /api/vendors/me/settings {business?: {...}, notifications?: {...}}

What a vendor may change here: the business name, a business phone / email /
address, and which booking alerts they receive. What they may NOT: their
sign-in phone (identity), role, KYC, active flag or venue approvals — those
stay with sign-in and the super admin. The account block is read-only.

alert_targets() is what the notification senders call, so the preferences
take effect on the next booking.
"""
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import VendorSettings, phone_validator

NOTIFICATION_FIELDS = {
    'bookingEmail': 'notify_booking_email',
    'bookingSms': 'notify_booking_sms',
    'ticketEmail': 'notify_ticket_email',
    'ticketSms': 'notify_ticket_sms',
}
TEXT_LIMITS = {'name': 150, 'address': 300}


def settings_of(user):
    """The vendor's settings row, or an unsaved one holding the defaults."""
    return VendorSettings.objects.filter(user=user).first() or VendorSettings(user=user)


def alert_targets(user, kind):
    """(email, phone) a booking ('booking') or ticket ('ticket') alert goes
    to — '' for a channel the vendor switched off. The business contact
    wins over the account's own when the vendor set one."""
    row = settings_of(user)
    email = row.business_email or user.email or ''
    phone = row.business_phone or user.phone or ''
    if kind == 'ticket':
        wants_email, wants_sms = row.notify_ticket_email, row.notify_ticket_sms
    else:
        wants_email, wants_sms = row.notify_booking_email, row.notify_booking_sms
    return (email if wants_email else ''), (phone if wants_sms else '')


def _account(user):
    from events.models import Event
    from venues.models import Listing, PayoutDetails

    listings = user.listings.exclude(status=Listing.Status.DELETED)
    venue_counts = {}
    for listing in listings:
        detail = (listing.record or {}).get('detail') or {}
        if isinstance(detail, dict) and detail.get('unitOf'):
            continue                      # a pitch/screen, not a venue
        venue_counts[listing.status] = venue_counts.get(listing.status, 0) + 1
    event_counts = {}
    for row in Event.objects.filter(vendor=user).exclude(status=Event.Status.DELETED).values('status'):
        event_counts[row['status']] = event_counts.get(row['status'], 0) + 1
    return {
        'phone': user.phone,
        'active': user.is_active,
        'kyc': user.kyc,
        'joinedAt': user.date_joined.isoformat() if user.date_joined else None,
        'venues': venue_counts,
        'events': event_counts,
        'payoutDetailsOnFile': PayoutDetails.objects.filter(user=user).exists(),
    }


def payload(user):
    row = settings_of(user)
    return {
        'profile': {'name': user.name, 'email': user.email, 'phone': user.phone},
        'business': {
            'name': row.business_name,
            'phone': row.business_phone,
            'email': row.business_email,
            'address': row.business_address,
        },
        'notifications': {key: getattr(row, field) for key, field in NOTIFICATION_FIELDS.items()},
        'account': _account(user),
    }


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


class VendorSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def _guard(self, request):
        if not request.user.has_vendor_access:
            return _message('Only vendor accounts can access this endpoint.',
                            status.HTTP_403_FORBIDDEN)
        return None

    def get(self, request):
        return self._guard(request) or Response(payload(request.user))

    def patch(self, request):
        denied = self._guard(request)
        if denied:
            return denied
        body = request.data if isinstance(request.data, dict) else {}
        row = settings_of(request.user)

        business = body.get('business')
        if business is not None:
            if not isinstance(business, dict):
                return _message('business must be an object.', status.HTTP_400_BAD_REQUEST)
            for key, limit in TEXT_LIMITS.items():
                if key in business:
                    text = str(business.get(key) or '').strip()
                    if len(text) > limit:
                        return _message(f'Business {key} can be at most {limit} characters.',
                                        status.HTTP_400_BAD_REQUEST)
                    setattr(row, f'business_{key}', text)
            if 'phone' in business:
                phone = str(business.get('phone') or '').strip()
                if phone:
                    try:
                        phone_validator(phone)
                    except ValidationError:
                        return _message('Enter a 10-digit mobile number.',
                                        status.HTTP_400_BAD_REQUEST)
                row.business_phone = phone
            if 'email' in business:
                email = str(business.get('email') or '').strip()
                if email:
                    try:
                        validate_email(email)
                    except ValidationError:
                        return _message('Enter a valid email address.',
                                        status.HTTP_400_BAD_REQUEST)
                row.business_email = email

        notifications = body.get('notifications')
        if notifications is not None:
            if not isinstance(notifications, dict):
                return _message('notifications must be an object.', status.HTTP_400_BAD_REQUEST)
            for key, field in NOTIFICATION_FIELDS.items():
                if key in notifications:
                    value = notifications[key]
                    if not isinstance(value, bool):
                        return _message(f'{key} must be true or false.',
                                        status.HTTP_400_BAD_REQUEST)
                    setattr(row, field, value)

        row.save()
        return Response(payload(request.user))
