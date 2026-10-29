"""
Vendor check-in endpoints — the portal's "Check-in" screen.

    GET  /api/vendors/me/check-ins          -> {gates, checkedInToday}
    POST /api/vendors/me/check-ins/verify   {bookingId, venueId | eventId} -> {result}
    POST /api/vendors/me/check-ins          {bookingId, venueId | eventId} -> {result}

Vendor JWT only (IsVendor). The gate — the venue or event being checked — is
resolved against the token owner's OWN listings / visible events, so a
foreign id is a 404. Every rule lives in bookings/checkin.py; the browser's
copy of a verdict is display only and can never admit anyone.

Booking IDs travel in the JSON body, not the URL, so they stay out of access
logs. Both POSTs are throttled per account (scope 'checkin').

Error shape: {"message": "..."}; a refused check-in is 409 and carries the
same `result` verdict the verify endpoint returns.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from venues.views import IsVendor

from . import checkin
from .views import _message


def _body(request):
    return request.data if isinstance(request.data, dict) else {}


def _booking_id(body):
    raw = str(body.get('bookingId') or '').strip()
    return raw if 0 < len(raw) <= checkin.MAX_INPUT else ''


def _actor(request):
    """Who minted the token when an admin is acting as this vendor
    (adminpanel AdminVendorTokenView stamps an `actor` claim). Logged only."""
    token = getattr(request, 'auth', None)
    try:
        return token.get('actor') if token is not None else None
    except AttributeError:
        return None


def _parse(request):
    """(booking_id, gate) or an error Response."""
    body = _body(request)
    booking_id = _booking_id(body)
    if not booking_id:
        return None, _message('Enter a booking ID.', status.HTTP_400_BAD_REQUEST)
    try:
        gate = checkin.resolve_gate(
            request.user, venue_id=body.get('venueId'), event_id=body.get('eventId'),
        )
    except checkin.GateError as error:
        code = (
            status.HTTP_400_BAD_REQUEST
            if bool(body.get('venueId')) == bool(body.get('eventId'))
            else status.HTTP_404_NOT_FOUND
        )
        return None, _message(str(error), code)
    return (booking_id, gate), None


class VendorCheckInView(APIView):
    """GET the gates this vendor can check in at; POST to admit a booking."""

    permission_classes = [IsVendor]
    throttle_scope = 'checkin'

    def get_throttles(self):
        # Reading the gate list is free; only verification attempts count.
        return [ScopedRateThrottle()] if self.request.method == 'POST' else []

    def get(self, request):
        return Response({
            'gates': checkin.vendor_gates(request.user),
            'checkedInToday': checkin.checkin_today_count(request.user),
        })

    def post(self, request):
        parsed, error = _parse(request)
        if error is not None:
            return error
        booking_id, gate = parsed
        verdict = checkin.vendor_check_in(request.user, booking_id, gate, actor=_actor(request))
        if verdict['code'] == checkin.CHECKED_IN:
            return Response({'result': verdict})
        return Response(
            {'message': verdict['message'], 'code': verdict['code'], 'result': verdict},
            status=status.HTTP_409_CONFLICT,
        )


class VendorCheckInVerifyView(APIView):
    """POST — what is this booking ID, and may it enter here? Never writes."""

    permission_classes = [IsVendor]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'checkin'

    def post(self, request):
        parsed, error = _parse(request)
        if error is not None:
            return error
        booking_id, gate = parsed
        return Response({
            'result': checkin.vendor_verify(request.user, booking_id, gate, actor=_actor(request)),
        })
