"""
The customer's own event tickets (User JWT).

Owner-scoped exactly like bookings: every query filters user=request.user, so
somebody else's order id simply 404s and can never be read.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.contact import order_contact

from bookings.slots import now_minutes_ist, today_ist

from .models import EventOrder

MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


class MyEventOrdersView(APIView):
    """GET /api/users/me/event-orders?status=upcoming|past&page=&limit="""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Every order the customer started, with its real outcome — paid,
        # awaiting payment, failed, cancelled, refunded — so a failed attempt
        # is never mistaken for a ticket. Only admin-removed rows are hidden.
        # Each record's `status` (display_status) says which it is; the
        # frontend shows a gate-ready ticket for confirmed/completed only.
        queryset = EventOrder.objects.filter(user=request.user).exclude(
            status='removed'
        ).select_related(
            'event', 'event__vendor',   # address + organiser contact
            'rating',                   # ratingStatus / userRating, no query per row
        )

        wanted = request.query_params.get('status')
        if wanted == 'upcoming':
            queryset = queryset.filter(date__gte=today_ist())
        elif wanted == 'past':
            queryset = queryset.filter(date__lt=today_ist())
        elif wanted:
            return _message(
                'status must be "upcoming" or "past".', status.HTTP_400_BAD_REQUEST
            )

        try:
            limit = int(request.query_params.get('limit', DEFAULT_LIMIT))
            page = int(request.query_params.get('page', 1))
        except ValueError:
            return _message('page and limit must be numbers.', status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= MAX_LIMIT) or page < 1:
            return _message('Invalid page or limit.', status.HTTP_400_BAD_REQUEST)

        total = queryset.count()
        rows = queryset[(page - 1) * limit:page * limit]
        return Response({
            # The organiser's name + phone — ONLY on a verified-paid order
            # (bookings.contact.order_contact).
            # ratingStatus: may this ticket be reviewed yet (bookings.ratings).
            'orders': [
                {**row.as_record(), 'vendorContact': order_contact(row), **row.rating_fields()}
                for row in rows
            ],
            'total': total,
        })


class CancelEventOrderView(APIView):
    """DELETE /api/users/me/event-orders/<id> — release an unpaid hold.

    Only a payment_pending hold may be dropped this way, which is what the
    checkout does when the customer dismisses the Razorpay widget: the seats
    free immediately instead of waiting out the 30-minute expiry.

    A CONFIRMED ticket is deliberately NOT self-service cancellable — the same
    rule the customer bookings page applies, because a refund has to go
    through the admin Refunds panel.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request, order_id):
        order = EventOrder.objects.filter(pk=order_id, user=request.user).first()
        if order is None:
            return _message('Order not found.', status.HTTP_404_NOT_FOUND)

        if order.status in ('cancelled', 'failed'):
            # Already released. A FAILED attempt stays 'failed' — dismissing
            # the widget afterwards must not relabel it as a cancellation.
            return Response({'cancelled': True, 'id': order_id})   # idempotent

        if order.status != 'payment_pending':
            return _message(
                'Paid tickets cannot be cancelled here — contact support.',
                status.HTTP_400_BAD_REQUEST,
            )

        today = today_ist()
        ended = order.date < today or (
            order.date == today
            and order.end_time
            and (order.end_time.hour * 60 + order.end_time.minute) <= now_minutes_ist()
        )
        if ended:
            return _message('This event has already finished.', status.HTTP_400_BAD_REQUEST)

        # Keep the row (never delete it) so a late payment.captured webhook can
        # still find it and auto-refund — deleting would strand the money.
        order.status = 'cancelled'
        order.save(update_fields=['status'])
        return Response({'cancelled': True, 'id': order_id})
