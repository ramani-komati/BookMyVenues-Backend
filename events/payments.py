"""
Event ticket payments — the same three-step Razorpay flow venue bookings use:

  1. POST /api/events/payments/order   — price the selection server-side, lock
     the date, hold the seats as a payment_pending order, create the gateway
     order.
  2. POST /api/events/payments/verify  — checkout callback: verify the HMAC
     signature, confirm the order.
  3. the SHARED /api/payments/webhook  — server-to-server source of truth.
     bookings/payments.py falls through to handle_event_webhook() below when
     an order id is not one of its bookings, so Razorpay keeps ONE configured
     webhook URL and no dashboard change is needed.

Nothing here can confirm tickets without the gateway, and no amount is ever
taken from the request body.
"""
import logging

from django.conf import settings as django_settings
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from bookings.razorpay_client import (
    RazorpayError,
    create_order,
    refund_payment,
    verify_payment_signature,
)
from bookings.views import _text

from .inventory import InventoryError, reserve_seats, seats_remaining
from .models import Event, EventOrder
from .pricing import PricingError, quote

logger = logging.getLogger(__name__)


def _message(text, http_status, code=''):
    body = {'message': text}
    if code:
        body['code'] = code
    return Response(body, status=http_status)


def _inventory_response(error):
    """409 for a sold-out date, 400 for an unbookable one — the frontend
    needs to tell "try fewer tickets" from "try another date"."""
    body = {'message': str(error), 'code': error.code or 'UNAVAILABLE'}
    if error.remaining is not None:
        body['remaining'] = error.remaining
    http_status = (
        status.HTTP_409_CONFLICT if error.code == 'SOLD_OUT'
        else status.HTTP_400_BAD_REQUEST
    )
    return Response(body, status=http_status)


def price_order_request(body):
    """Validate + price a ticket request. Returns (data, None) or (None, resp).

    Shared by the quote endpoint and the order endpoint so the number the
    customer is shown and the number the gateway charges come from one place.
    """
    event_ref = str(body.get('eventId') or body.get('eventSlug') or '').strip()
    event = None
    if event_ref:
        event = (
            Event.objects.filter(pk=event_ref).first()
            if _looks_like_uuid(event_ref)
            else Event.objects.filter(slug=event_ref).first()
        )
    if event is None:
        return None, _message('Event not found.', status.HTTP_404_NOT_FOUND)
    if event.status not in Event.BOOKABLE_STATUSES:
        return None, _message(
            'This event is not open for booking.', status.HTTP_409_CONFLICT,
            code='EVENT_UNAVAILABLE',
        )

    event_date = _resolve_date(event, body)
    if event_date is None:
        return None, _message(
            'This event is not running on that date.',
            status.HTTP_404_NOT_FOUND, code='DATE_NOT_FOUND',
        )

    try:
        priced = quote(event, body.get('tickets'))
    except PricingError as error:
        return None, _message(str(error), status.HTTP_400_BAD_REQUEST, code=error.code)

    # The client may send its computed total; it is CROSS-CHECKED, never used.
    # Charging silently differs from what the customer saw would be worse than
    # making them retry, so a genuine mismatch is surfaced with our figure.
    client_amount = body.get('amount')
    if client_amount not in (None, ''):
        try:
            if int(str(client_amount)) != priced['amount']:
                return None, Response(
                    {
                        'message': f'Amount mismatch: expected ₹{priced["amount"]}.',
                        'code': 'AMOUNT_MISMATCH',
                        'expectedAmount': priced['amount'],
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
        except (TypeError, ValueError):
            return None, _message(
                'amount must be a number.', status.HTTP_400_BAD_REQUEST
            )

    return {'event': event, 'event_date': event_date, **priced}, None


def _looks_like_uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _resolve_date(event, body):
    """The EventDate the customer picked, by id or by YYYY-MM-DD."""
    raw_id = body.get('eventDateId') or body.get('dateId')
    if raw_id not in (None, ''):
        try:
            return event.dates.filter(pk=int(str(raw_id))).first()
        except (TypeError, ValueError):
            return None
    raw_date = str(body.get('date') or '').strip()
    if not raw_date:
        return None
    from bookings.slots import SlotError, parse_date
    try:
        return event.dates.filter(date=parse_date(raw_date)).first()
    except SlotError:
        return None


def build_order_fields(user, body, data):
    """The EventOrder.objects.create kwargs — all prices server-computed."""
    event = data['event']
    return dict(
        event=event,
        event_date=data['event_date'],
        user=user,
        event_name=event.name,
        event_category=event.category,
        location=event.location,
        image=event.poster_url,
        date=data['event_date'].date,
        start_time=event.start_time,
        end_time=event.end_time,
        customer_name=_text(body.get('customer'), 150) or user.name,
        phone=user.phone,
        items=data['items'],
        seats=data['seats'],
        ticket_count=data['ticketCount'],
        subtotal=data['subtotal'],
        fee=data['fee'],
        amount=data['amount'],
    )


class EventQuoteView(APIView):
    """POST /api/events/quote — the server's price for a ticket selection.

    Exists so the booking screen shows the SAME breakdown the order will
    charge, instead of recomputing the fee client-side and drifting when an
    admin changes it.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        # The quote must not reject on amount: nothing has been priced yet.
        body = {k: v for k, v in body.items() if k != 'amount'}
        data, error = price_order_request(body)
        if error is not None:
            return error
        return Response({
            'items': data['items'],
            'ticketCount': data['ticketCount'],
            'seats': data['seats'],
            'subtotal': data['subtotal'],
            'fee': data['fee'],
            'amount': data['amount'],
            'remaining': seats_remaining(data['event_date']),
        })


class EventPaymentOrderView(APIView):
    """POST /api/events/payments/order — ticket selection in, gateway order out."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        data, error = price_order_request(body)
        if error is not None:
            return error

        # Fail fast on an obviously sold-out date BEFORE calling the gateway,
        # so a sold-out event does not mint orphan Razorpay orders. The real
        # guarantee is the locked re-check below — this is only politeness.
        try:
            reserve_seats(
                data['event'], data['event_date'].id, data['seats']
            )
        except InventoryError as inventory_error:
            return _inventory_response(inventory_error)

        try:
            order_id = create_order(data['amount'], receipt=request.user.phone)
        except RazorpayError as exc:
            return _message(str(exc), status.HTTP_502_BAD_GATEWAY)

        # THE oversell guarantee: re-check under the date's row lock and
        # create the holding order inside the SAME transaction, so two
        # requests for the last seats are serialised and the loser sees 409.
        try:
            with transaction.atomic():
                reserve_seats(data['event'], data['event_date'].id, data['seats'])
                order = EventOrder.objects.create(
                    **build_order_fields(request.user, body, data),
                    status='payment_pending',      # holds the seats
                    razorpay_order_id=order_id,
                )
        except InventoryError as inventory_error:
            # The gateway order is orphaned and unpaid — harmless, it expires.
            return _inventory_response(inventory_error)

        return Response({
            'orderId': order_id,
            'keyId': django_settings.RAZORPAY_KEY_ID,
            'amount': data['amount'] * 100,   # paise, as the widget expects
            'eventOrderId': order.id,
            'breakdown': {
                'items': data['items'],
                'subtotal': data['subtotal'],
                'fee': data['fee'],
                'amount': data['amount'],
            },
        }, status=status.HTTP_201_CREATED)


def _confirm_order(order, payment_id):
    """Move a pending order to confirmed. Idempotent — a webhook redelivery
    and the checkout callback both land here and only one sends the email."""
    already_confirmed = order.status == 'confirmed'
    order.status = 'confirmed'
    order.razorpay_payment_id = payment_id or order.razorpay_payment_id
    order.save(update_fields=['status', 'razorpay_payment_id'])
    if order.user_id and not order.user.is_customer:
        order.user.is_customer = True
        order.user.save(update_fields=['is_customer'])
    if not already_confirmed and order.confirmation_sent_at is None:
        _notify_confirmed(order)


def _notify_confirmed(order):
    from django.utils import timezone as _tz

    from .notifications import notify_tickets_confirmed
    try:
        if notify_tickets_confirmed(order):
            order.confirmation_sent_at = _tz.now()
            order.save(update_fields=['confirmation_sent_at'])
    except Exception:                      # never break a paid order
        logger.exception('Event ticket confirmation notification failed')


class EventPaymentVerifyView(APIView):
    """POST /api/events/payments/verify — the checkout success callback."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        order_id = str(body.get('razorpay_order_id') or '')
        payment_id = str(body.get('razorpay_payment_id') or '')

        # Owner-scoped: someone else's order id looks like it does not exist.
        order = EventOrder.objects.filter(
            pk=str(body.get('eventOrderId') or body.get('orderRef') or ''),
            user=request.user,
            razorpay_order_id=order_id,
        ).first()
        if order is None:
            return _message('Order not found.', status.HTTP_404_NOT_FOUND)

        if not verify_payment_signature(
            order_id, payment_id, body.get('razorpay_signature')
        ):
            # Order stays unconfirmed — the webhook remains the arbiter.
            return _message('Invalid payment signature.', status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            order.refresh_from_db()
            if order.status == 'confirmed':
                return Response({'order': order.as_record()})   # idempotent
            if order.status != 'payment_pending':
                # cancelled/refunded — a replayed signature must NEVER revive it.
                return _message(
                    'This order can no longer be confirmed.', status.HTTP_409_CONFLICT
                )
            if not _still_has_room(order):
                # The hold expired and the seats were resold mid-payment.
                order.status = 'refund_pending'   # admin Refunds panel picks it up
                order.razorpay_payment_id = payment_id
                order.save(update_fields=['status', 'razorpay_payment_id'])
                return _message(
                    'Payment received but this date sold out meanwhile — '
                    'our team will refund you.',
                    status.HTTP_409_CONFLICT,
                )
            _confirm_order(order, payment_id)

        return Response({'order': order.as_record()})


def _still_has_room(order):
    """True when the order's own seats are still available, ignoring itself.

    Called inside a transaction; locks the date so the answer cannot change
    between the check and the confirmation that follows it.
    """
    from .models import EventDate
    from .inventory import seats_remaining

    if order.event_date_id is None:
        return False
    locked = EventDate.objects.select_for_update().filter(
        pk=order.event_date_id
    ).first()
    if locked is None:
        return False
    return order.seats <= seats_remaining(locked, exclude_order_id=order.pk)


def _auto_refund(order, payment_id):
    """Money captured for an order the customer already cancelled: refund it
    and record it. The order stays cancelled — never revived. Mirrors
    bookings.payments._auto_refund so both products behave the same."""
    order.razorpay_payment_id = payment_id or order.razorpay_payment_id
    try:
        order.refund_id = refund_payment(order.razorpay_payment_id)
        order.status = 'refunded'
        order.refund_reason = order.refund_reason or 'Checkout abandoned — auto-refunded'
        order.refund_amount = order.amount
    except RazorpayError:
        logger.exception(
            'Auto-refund FAILED for cancelled event order %s (payment %s) — '
            'queued for manual refund', order.id, order.razorpay_payment_id,
        )
        order.status = 'refund_pending'
    order.save(update_fields=[
        'status', 'razorpay_payment_id', 'refund_id', 'refund_reason', 'refund_amount',
    ])


def handle_event_webhook(event_name, entity):
    """Razorpay webhook fallthrough for event orders.

    Called by bookings.payments.RazorpayWebhookView AFTER its signature check
    passes and no Booking matches the order id. Returns True when this module
    owned the order (so the webhook answers 'ok'), False when it did not.

    Sharing the endpoint keeps ONE webhook URL configured at Razorpay — adding
    a second one would be a production dashboard change, not a code change.
    """
    order_id = str(entity.get('order_id') or '')
    payment_id = str(entity.get('id') or '')
    order = EventOrder.objects.filter(razorpay_order_id=order_id).first()
    if order is None:
        return False

    if event_name == 'payment.captured':
        # Defence in depth: never confirm for a different amount than ours.
        entity_amount = entity.get('amount')
        if entity_amount is not None:
            try:
                if int(entity_amount) != order.amount * 100:
                    return True   # ours, but refused — do not confirm
            except (TypeError, ValueError):
                return True
        with transaction.atomic():
            order.refresh_from_db()
            if order.status in EventOrder.DEAD_STATUSES:
                # Customer abandoned the widget but the UPI collect landed
                # anyway. NEVER resurrect the order — refund the capture.
                _auto_refund(order, payment_id)
            elif order.status == 'payment_pending':
                if not _still_has_room(order):
                    order.status = 'refund_pending'
                    order.razorpay_payment_id = payment_id
                    order.save(update_fields=['status', 'razorpay_payment_id'])
                else:
                    _confirm_order(order, payment_id)
    elif event_name == 'payment.failed':
        if order.status == 'payment_pending':
            order.status = 'cancelled'    # releases the held seats
            order.save(update_fields=['status'])
    return True
