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
import logging

from django.conf import settings as django_settings
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.business import check_bookable
from venues.models import Listing

from . import payment_record
from .models import Booking
from .razorpay_client import (
    RazorpayError,
    create_order,
    refund_payment,
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

logger = logging.getLogger(__name__)


def _conflicts_excluding_self(booking):
    """True when another ACTIVE booking overlaps this one (hold may have
    expired and the slot been re-sold before payment finished)."""
    try:
        intervals = parse_slots(booking.slots)
    except SlotError:
        return False
    # A vendor block placed after this hold expired took the time back too
    # (venues/business.py refuses blocks over LIVE holds only).
    from venues.business import blocks_for
    blocked = [
        (block.start_minute, block.end_minute)
        for block in blocks_for(booking.listing, booking.date)
    ]
    if blocked and overlaps(intervals, blocked):
        return True
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
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        data, error = validate_booking_request(body, user=request.user)
        if error is not None:
            return error
        listing, date = data['listing'], data['date']

        # Create the gateway order BEFORE taking the lock (external call).
        # An orphaned unpaid order is harmless if the overlap check fails.
        try:
            # Part payment charges only the online slice; without a split
            # this IS the full amount.
            order_id = create_order(data['pay_now'], receipt=request.user.phone)
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
            # Hours / closures / blocks again, now under the lock: a vendor
            # block saved since validation must win over this order.
            try:
                check_bookable(listing, date, data['intervals'])
            except SlotError as error:
                return Response(
                    {'message': str(error), 'code': error.code},
                    status=status.HTTP_409_CONFLICT,
                )
            booking = Booking.objects.create(
                **build_booking_fields(request.user, body, data),
                status='payment_pending',      # soft-holds the slot
                razorpay_order_id=order_id,
            )

        return Response({
            'orderId': order_id,
            'keyId': django_settings.RAZORPAY_KEY_ID,
            'amount': data['pay_now'] * 100,  # paise, as the widget expects
            'payNow': data['pay_now'],
            'atVenue': data['at_venue'],
            'bookingId': booking.id,
        }, status=status.HTTP_201_CREATED)


def _confirm_booking(booking, payment_id, *, paid_at=None, source=payment_record.CHECKOUT):
    """Move a pending booking to confirmed (idempotent), recording when the
    successful payment happened (bookings/payment_record.py)."""
    already_confirmed = booking.status == 'confirmed'
    booking.status = 'confirmed'
    booking.razorpay_payment_id = payment_id or booking.razorpay_payment_id
    booking.save(update_fields=['status', 'razorpay_payment_id'] + payment_record.stamp_payment_time(
        booking, booking.razorpay_payment_id, when=paid_at, source=source,
    ))
    if booking.user_id and not booking.user.is_customer:
        booking.user.is_customer = True
        booking.user.save(update_fields=['is_customer'])
    # Tell the customer and the vendor. Best-effort and idempotent: a webhook
    # re-delivery must not send a second copy, and a failed send must never
    # fail the payment — the scheduled catch-up run retries it.
    if not already_confirmed and booking.confirmation_sent_at is None:
        _notify_confirmed(booking)


def _notify_confirmed(booking):
    from django.utils import timezone as _tz

    from .notifications import notify_booking_confirmed
    try:
        if notify_booking_confirmed(booking):
            booking.confirmation_sent_at = _tz.now()
            booking.save(update_fields=['confirmation_sent_at'])
    except Exception:                      # never break a paid booking
        logger.exception('Booking confirmation notification failed')


class PaymentVerifyView(APIView):
    """POST /api/payments/verify — the checkout success callback."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

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
            booking.refresh_from_db()
            if booking.status == 'confirmed':
                return Response({'booking': booking.as_record()})  # idempotent
            if booking.status not in Booking.CONFIRMABLE_STATUSES:
                # refunded/cancelled/removed — a replayed signature must
                # NEVER revive it. ('failed' stays confirmable: a retry in
                # the same checkout can still succeed.)
                return _message(
                    'This booking can no longer be confirmed.',
                    status.HTTP_409_CONFLICT,
                )
            if _conflicts_excluding_self(booking):
                # Hold expired and the slot was re-sold mid-payment (rare).
                booking.status = 'refund_pending'  # admin Refunds panel picks it up
                booking.razorpay_payment_id = payment_id
                # The money WAS captured — record when, for the refund trail.
                booking.save(update_fields=['status', 'razorpay_payment_id'] + payment_record.stamp_payment_time(
                    booking, payment_id, source=payment_record.CHECKOUT,
                ))
                return _message(
                    'Payment received but the slot was re-booked meanwhile — '
                    'our team will refund you.',
                    status.HTTP_409_CONFLICT,
                )
            _confirm_booking(booking, payment_id)

        return Response({'booking': booking.as_record()})


def _auto_refund(booking, payment_id, *, paid_at=None, source=payment_record.WEBHOOK):
    """Money captured for a booking the customer already cancelled: refund it
    at the gateway and record it. The booking stays cancelled — never revived.
    A gateway failure leaves status 'refund_pending' so the admin Refunds
    panel picks it up (money is never silently kept). When the capture
    happened is recorded too: it is part of the refund's audit trail."""
    booking.razorpay_payment_id = payment_id or booking.razorpay_payment_id
    paid_fields = payment_record.stamp_payment_time(
        booking, booking.razorpay_payment_id, when=paid_at, source=source,
    )
    try:
        booking.refund_id = refund_payment(booking.razorpay_payment_id)
        booking.status = 'refunded'
        booking.refund_reason = booking.refund_reason or 'Checkout abandoned — auto-refunded'
        # Only what the gateway actually took — the at-venue slice was never
        # captured, so it cannot be refunded.
        booking.refund_amount = booking.online_amount
    except RazorpayError:
        logger.exception(
            'Auto-refund FAILED for cancelled booking %s (payment %s) — '
            'queued for manual refund', booking.id, booking.razorpay_payment_id,
        )
        booking.status = 'refund_pending'
    booking.save(update_fields=[
        'status', 'razorpay_payment_id', 'refund_id', 'refund_reason', 'refund_amount',
    ] + paid_fields)


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
        except ValueError:
            return _message('Malformed webhook payload.', status.HTTP_400_BAD_REQUEST)

        # Events we don't handle (disputes, downtime notices, authorized…)
        # are acknowledged with 200 — answering 4xx would make Razorpay
        # retry and eventually auto-disable the webhook.
        event = str(payload.get('event') or '')
        if event not in ('payment.captured', 'payment.failed'):
            return Response({'status': 'ignored'})

        try:
            entity = payload['payload']['payment']['entity']
        except (KeyError, TypeError):
            return _message('Malformed webhook payload.', status.HTTP_400_BAD_REQUEST)

        order_id = str(entity.get('order_id') or '')
        payment_id = str(entity.get('id') or '')
        # Razorpay's own time for this event (signed with the payload) — for
        # payment.captured, when the capture happened. None if unusable.
        captured_at = payment_record.gateway_time(payload)
        booking = Booking.objects.filter(razorpay_order_id=order_id).first()
        if booking is None:
            # Not a venue booking — it may be an EVENT ticket order. Both
            # products share this one webhook URL on purpose: adding a second
            # one would be a Razorpay dashboard change in production, whereas
            # this fall-through is a code change we can test. Imported locally
            # so the bookings app never depends on events at load time.
            from events.payments import handle_event_webhook
            if handle_event_webhook(event, entity, gateway_moment=captured_at):
                return Response({'status': 'ok'})
            return Response({'status': 'ignored'})  # not one of ours

        if event == 'payment.captured':
            # Defense in depth: never confirm for a different amount than ours.
            entity_amount = entity.get('amount')
            if entity_amount is not None:
                try:
                    # Compare against the ONLINE charge, not the full total:
                    # a part-paid booking captures only its payNow slice, and
                    # checking against `amount` would reject every one of them.
                    if int(entity_amount) != booking.online_amount * 100:
                        return Response({'status': 'amount-mismatch'})
                except (TypeError, ValueError):
                    return Response({'status': 'amount-mismatch'})
            paid_at, source = payment_record.webhook_time(captured_at)
            with transaction.atomic():
                if booking.listing_id:
                    Listing.objects.select_for_update().get(pk=booking.listing_id)
                booking.refresh_from_db()
                if booking.status in ('cancelled', 'refunded', 'removed'):
                    # The customer abandoned/cancelled the checkout (or an
                    # admin removed the unpaid booking) but the payment landed
                    # anyway (UPI-collect: approved in the UPI app after
                    # closing the widget). NEVER resurrect the booking —
                    # refund the capture instead.
                    _auto_refund(booking, payment_id, paid_at=paid_at, source=source)
                elif booking.status in Booking.CONFIRMABLE_STATUSES:
                    # payment_pending, or 'failed' then retried successfully
                    # inside the same checkout.
                    if booking.listing_id and _conflicts_excluding_self(booking):
                        # Late capture after the hold expired and the slot was
                        # re-sold — same rule as verify: queue for refund.
                        booking.status = 'refund_pending'
                        booking.razorpay_payment_id = payment_id
                        booking.save(update_fields=['status', 'razorpay_payment_id'] + payment_record.stamp_payment_time(
                            booking, payment_id, when=paid_at, source=source,
                        ))
                    else:
                        _confirm_booking(booking, payment_id, paid_at=paid_at, source=source)
                elif source == payment_record.GATEWAY:
                    # Already accounted for (the checkout callback confirmed
                    # it first, or this is a redelivery): nothing about the
                    # booking changes, but Razorpay's own capture time
                    # refines the server-side one — for the SAME payment only.
                    fields = payment_record.stamp_payment_time(
                        booking, payment_id, when=paid_at, source=source,
                    )
                    if fields:
                        booking.save(update_fields=fields)
        elif event == 'payment.failed':
            # One failed ATTEMPT. Recorded as 'failed' (releases the held
            # slot), not 'cancelled': the booking stays confirmable because
            # the customer may retry inside the same checkout and succeed.
            # Only a pending row moves — a confirmed/refunded/cancelled one
            # is never overwritten by a stale failure.
            with transaction.atomic():
                if booking.listing_id:
                    Listing.objects.select_for_update().get(pk=booking.listing_id)
                booking.refresh_from_db()
                if booking.status == 'payment_pending':
                    booking.status = 'failed'
                    booking.save(update_fields=['status'])

        return Response({'status': 'ok'})
