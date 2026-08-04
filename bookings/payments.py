"""
Razorpay payment flow (test mode and live use the same code — env keys decide).

  1. POST /api/payments/order   — validate + price the booking draft, create a
     Razorpay Order, soft-hold the slot as a payment_pending booking.
  2. POST /api/payments/verify  — checkout callback: verify the HMAC signature,
     confirm the booking.
  3. POST /api/payments/webhook — server-to-server source of truth: confirm on
     payment.captured, release the hold on payment.failed.

Pay-at-venue and walk-ins bypass all of this. The soft hold expires after
PENDING_HOLD_MINUTES (see _active_bookings) so abandoned checkouts free their
slots automatically.
"""
import json

from django.conf import settings as django_settings
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.models import Listing

from .models import Booking
from .razorpay_client import (
    RazorpayError,
    create_order,
    verify_payment_signature,
    verify_webhook_signature,
)
from .slots import SlotError, overlaps, parse_slots
from .views import (
    _active_bookings,
    _booked_intervals,
    _message,
    build_booking_fields,
    validate_booking_request,
)


def _conflicts_excluding_self(booking):
    """True when another ACTIVE booking overlaps this one (hold may have
    expired and the slot been re-sold before payment finished)."""
    try:
        intervals = parse_slots(booking.slots)
    except SlotError:
        return False
    others = []
    for other in _active_bookings(booking.listing, booking.date):
        if other.pk == booking.pk:
            continue
        if booking.unit is not None and other.unit is not None:
            if other.unit != booking.unit or (other.sport or '') != (booking.sport or ''):
                continue
        try:
            others.extend(parse_slots(other.slots))
        except SlotError:
            continue
    return overlaps(intervals, others)


class PaymentOrderView(APIView):
    """POST /api/payments/order — booking draft in, Razorpay order out."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        data, error = validate_booking_request(body)
        if error is not None:
            return error
        listing, date = data['listing'], data['date']

        # Create the gateway order BEFORE taking the lock (external call).
        # An orphaned unpaid order is harmless if the overlap check fails.
        try:
            order_id = create_order(data['amount'], receipt=request.user.phone)
        except RazorpayError as exc:
            return _message(str(exc), status.HTTP_502_BAD_GATEWAY)

        with transaction.atomic():
            Listing.objects.select_for_update().get(pk=listing.pk)
            if overlaps(
                data['intervals'],
                _booked_intervals(listing, date, data['sport'], data['unit']),
            ):
                return _message(
                    'One or more selected time slots were just booked. '
                    'Please pick different slots.',
                    status.HTTP_409_CONFLICT,
                )
            booking = Booking.objects.create(
                **build_booking_fields(request.user, body, data),
                status='payment_pending',      # soft-holds the slot
                razorpay_order_id=order_id,
            )

        return Response({
            'orderId': order_id,
            'keyId': django_settings.RAZORPAY_KEY_ID,
            'amount': data['amount'] * 100,   # paise, as the widget expects
            'bookingId': booking.id,
        }, status=status.HTTP_201_CREATED)


def _confirm_booking(booking, payment_id):
    """Move a pending booking to confirmed (idempotent)."""
    booking.status = 'confirmed'
    booking.razorpay_payment_id = payment_id or booking.razorpay_payment_id
    booking.save(update_fields=['status', 'razorpay_payment_id'])
    if booking.user_id and not booking.user.is_customer:
        booking.user.is_customer = True
        booking.user.save(update_fields=['is_customer'])


class PaymentVerifyView(APIView):
    """POST /api/payments/verify — the checkout success callback."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        order_id = str(body.get('razorpay_order_id') or '')
        payment_id = str(body.get('razorpay_payment_id') or '')

        booking = Booking.objects.filter(
            pk=str(body.get('bookingId') or ''),
            user=request.user,
            razorpay_order_id=order_id,
        ).first()
        if booking is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)

        if not verify_payment_signature(
            order_id, payment_id, body.get('razorpay_signature')
        ):
            # Booking stays unconfirmed — the webhook remains the arbiter.
            return _message('Invalid payment signature.', status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            Listing.objects.select_for_update().get(pk=booking.listing_id)
            if booking.status == 'payment_pending' and _conflicts_excluding_self(booking):
                # Hold expired and the slot was re-sold mid-payment (rare).
                return _message(
                    'Payment received but the slot was re-booked meanwhile — '
                    'our team will refund you.',
                    status.HTTP_409_CONFLICT,
                )
            _confirm_booking(booking, payment_id)

        return Response({'booking': booking.as_record()})


class RazorpayWebhookView(APIView):
    """POST /api/payments/webhook — signature-verified source of truth."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        signature = request.headers.get('X-Razorpay-Signature', '')
        if not verify_webhook_signature(request.body, signature):
            return _message('Invalid webhook signature.', status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(request.body)
            event = str(payload.get('event') or '')
            entity = payload['payload']['payment']['entity']
        except (ValueError, KeyError, TypeError):
            return _message('Malformed webhook payload.', status.HTTP_400_BAD_REQUEST)

        order_id = str(entity.get('order_id') or '')
        payment_id = str(entity.get('id') or '')
        booking = Booking.objects.filter(razorpay_order_id=order_id).first()
        if booking is None:
            return Response({'status': 'ignored'})  # not one of ours

        if event == 'payment.captured':
            if booking.status == 'payment_pending':
                _confirm_booking(booking, payment_id)
        elif event == 'payment.failed':
            if booking.status == 'payment_pending':
                booking.status = 'cancelled'  # releases the held slot
                booking.save(update_fields=['status'])

        return Response({'status': 'ok'})
