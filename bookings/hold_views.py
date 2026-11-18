"""
The 5-minute slot hold endpoints (rules: bookings/holds.py).

    POST   /api/bookings/holds        reserve the chosen slot(s) for 5 minutes
    GET    /api/bookings/holds/<id>   my hold's state and the server's expiry
    DELETE /api/bookings/holds/<id>   let it go (changed my mind / went back)

The customer's checkout calls POST when it leaves the slot step, shows the
countdown from the answer, and hands the hold's id to POST /api/payments/order,
which turns the SAME row into the payment (never extending it). Celebrate,
Gaming, Sports and resort hourly bookings all go through here; resort
packages / surprises hold at their own checkout (offerings/views.py) with the
same 5-minute expiry.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from venues.business import check_bookable
from venues.models import Listing

from . import holds
from .models import Booking
from .slots import SlotError, overlaps
from .views import _active_bookings, _message, build_booking_fields, validate_booking_request

HELD_MESSAGE = 'This slot is temporarily reserved. Please select another slot.'
BOOKED_MESSAGE = 'One or more selected time slots were just booked. Please pick different slots.'


def conflict_response(listing, date, data, exclude_pk=None):
    """409 naming WHY the slot is unavailable: SLOT_HELD when only someone's
    live checkout hold is in the way (it may come free in a few minutes),
    SLOT_BOOKED when a confirmed booking is. Never says WHO holds it."""
    from .slots import SlotError as _SlotError
    from .slots import parse_slots

    excluded = set(exclude_pk) if isinstance(exclude_pk, (set, frozenset, list, tuple)) else {exclude_pk}
    only_holds = True
    for other in _active_bookings(listing, date):
        if other.pk in excluded:
            continue
        if data['unit'] is not None and other.unit is not None:
            if other.unit != data['unit'] or (other.sport or '') != (data['sport'] or ''):
                continue
        try:
            if not overlaps(data['intervals'], parse_slots(other.slots)):
                continue
        except _SlotError:
            continue
        if other.status != 'payment_pending':
            only_holds = False
            break
    return Response(
        {
            'message': HELD_MESSAGE if only_holds else BOOKED_MESSAGE,
            'code': 'SLOT_HELD' if only_holds else 'SLOT_BOOKED',
        },
        status=status.HTTP_409_CONFLICT,
    )


def _same_reservation(hold, data):
    return (
        hold.listing_id == data['listing'].pk
        and hold.date == data['date']
        and list(hold.slots or []) == list(data['slots'])
        and (hold.sport or '') == (data['sport'] or '')
        and hold.unit == data['unit']
    )


class HoldCreateView(APIView):
    """POST /api/bookings/holds — body: the booking draft (venueId, date,
    slots, sport/unit, addons…), as for POST /api/payments/order."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        data, error = validate_booking_request(body, user=request.user, hold_only=True)
        if error is not None:
            return error
        listing, date = data['listing'], data['date']

        from .ratings import venue_set_ids

        with transaction.atomic():
            # THE concurrency guard: every hold, booking and walk-in for this
            # venue queues behind this row lock, and the overlap check below is
            # re-read inside it — two customers can never both get the slot.
            Listing.objects.select_for_update().get(pk=listing.pk)

            mine = list(
                Booking.objects.filter(holds.live_hold_q(), user=request.user, kind='')
                .filter(listing_id__in=venue_set_ids(listing))
            )
            # The same reservation again (refresh, second device, double
            # click): the SAME hold with the SAME expiry — never a new 5 minutes.
            for hold in mine:
                if _same_reservation(hold, data):
                    return Response({'hold': holds.hold_payload(hold)})

            # A different choice at this venue replaces the customer's earlier
            # hold there (unless its payment has already started) — but only
            # once the new one is granted: a refused attempt must never cost
            # the customer the slot they already hold.
            replaced = [h for h in mine if not h.razorpay_order_id]
            replaced_pks = {h.pk for h in replaced}

            others = Booking.objects.filter(holds.live_hold_q(), user=request.user).exclude(
                pk__in=replaced_pks,
            ).count()
            if others >= holds.MAX_LIVE_HOLDS_PER_USER:
                return _message(
                    'You already have slots reserved at other venues. Finish or cancel '
                    'one of those bookings first.',
                    status.HTTP_429_TOO_MANY_REQUESTS,
                )

            from .views import _booked_intervals
            taken = _booked_intervals(listing, date, data['sport'], data['unit'], exclude_pk=replaced_pks)
            if overlaps(data['intervals'], taken):
                return conflict_response(listing, date, data, exclude_pk=replaced_pks)
            try:
                check_bookable(listing, date, data['intervals'])
            except SlotError as error:
                return Response({'message': str(error), 'code': error.code}, status=status.HTTP_409_CONFLICT)

            for old in replaced:
                old.status = 'cancelled'
                old.save(update_fields=['status'])
            hold = Booking.objects.create(
                **build_booking_fields(request.user, body, data),
                status='payment_pending',
                hold_expires_at=holds.new_expiry(),
            )

        return Response({'hold': holds.hold_payload(hold)}, status=status.HTTP_201_CREATED)


class HoldDetailView(APIView):
    """GET / DELETE /api/bookings/holds/<id> — owner only (someone else's id
    looks like it does not exist)."""

    permission_classes = [IsAuthenticated]

    def _hold(self, request, hold_id):
        return Booking.objects.filter(pk=hold_id, user=request.user, kind='').first()

    def get(self, request, hold_id):
        hold = self._hold(request, hold_id)
        if hold is None:
            return _message('Reservation not found.', status.HTTP_404_NOT_FOUND)
        return Response({'hold': holds.hold_payload(hold)})

    def delete(self, request, hold_id):
        with transaction.atomic():
            hold = self._hold(request, hold_id)
            if hold is None:
                return _message('Reservation not found.', status.HTTP_404_NOT_FOUND)
            if hold.status == 'payment_pending':
                # Kept (not deleted) so a late payment against an order it may
                # carry is found and refunded (bookings/payments.py).
                hold.status = 'cancelled'
                hold.save(update_fields=['status'])
            elif hold.status != 'cancelled':
                return _message('This booking is no longer a reservation.', status.HTTP_409_CONFLICT)
        return Response({'released': True, 'hold': holds.hold_payload(hold)})
