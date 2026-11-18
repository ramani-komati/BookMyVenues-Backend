"""
POST /api/admin/event-orders/<id>/resend-tickets   {"confirm": true}

Send the customer's ticket email again, with the PDF tickets attached — for a
customer who lost it or whose first email failed. Safe by construction:

  * only a VERIFIED PAID order (confirmed + Razorpay payment id) has tickets;
    anything else is refused (409) and nothing is sent;
  * customer email only — no SMS, no vendor alert, no second "sale";
  * an explicit {"confirm": true}, and at most one resend per order every
    RESEND_COOLDOWN_MINUTES (a double click or two admins at once cannot
    send twice: the cooldown is checked and recorded under the order's row
    lock);
  * every resend is written to the audit log (who, when, which order);
  * the order's own confirmation_sent_at is untouched — it records the
    FIRST confirmation and keeps doing its job of stopping webhook retries
    from sending duplicates.
"""
import datetime

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from events import tickets
from events.models import EventOrder

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from .models import AuditEntry

RESEND_COOLDOWN_MINUTES = 10
ACTION = 'Resent event tickets'


def last_resend(order_id):
    entry = AuditEntry.objects.filter(action=ACTION, target_id=str(order_id)).order_by('-created_at').first()
    return entry.created_at if entry else None


class AdminResendTicketsView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [JSONParser]

    def post(self, request, order_id):
        from events.notifications import send_customer_tickets

        from .views import record_audit

        body = request.data if isinstance(request.data, dict) else {}
        if body.get('confirm') is not True:
            return detail('Confirm the resend first.', status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            order = EventOrder.objects.select_for_update().select_related('user', 'event').filter(
                pk=order_id,
            ).first()
            if order is None:
                return detail('Order not found.', status.HTTP_404_NOT_FOUND)
            if not tickets.issuable(order):
                return Response(
                    {'detail': 'Only a paid, confirmed order has valid tickets — nothing was sent.',
                     'code': 'NOT_ISSUABLE'},
                    status=status.HTTP_409_CONFLICT,
                )
            email = getattr(order.user, 'email', '') or ''
            if not email:
                return Response(
                    {'detail': 'This customer has no email address on their account.',
                     'code': 'NO_EMAIL'},
                    status=status.HTTP_409_CONFLICT,
                )
            previous = last_resend(order.pk)
            wait = datetime.timedelta(minutes=RESEND_COOLDOWN_MINUTES)
            if previous and timezone.now() - previous < wait:
                return Response(
                    {'detail': f'Tickets were resent less than {RESEND_COOLDOWN_MINUTES} minutes ago. '
                               'Please wait before sending again.',
                     'code': 'COOLDOWN',
                     'retryAfter': (previous + wait).isoformat()},
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )
            sent = send_customer_tickets(order)
            if sent:
                record_audit(
                    request, ACTION, order.event_name or 'Event order',
                    f'#{order.id} · {order.ticket_count} ticket(s) · to the customer\'s email',
                    target_id=order.pk,
                )
        if not sent:
            return Response(
                {'detail': 'The email could not be sent (email delivery is off or failed). '
                           'Nothing was recorded — try again later.',
                 'code': 'SEND_FAILED'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response({'sent': True, 'orderId': order.pk, 'tickets': order.ticket_count})
