"""
Super-admin event management — the ONLY place an event is created or edited.

The client moved events off the vendor workflow entirely: the admin creates
them from the admin dashboard, with the organiser recorded as plain text, and
no vendor onboarding, bank account or payout setup is involved. Vendors have
no write path at all (see events/vendor_views.py, which now refuses every
mutation with 403).

These views live in the EVENTS app rather than adminpanel/views.py because
everything they touch — the capacity rules, the ticket-type rules, the
slug — lives here, and adminpanel/views.py is already 1 000 lines. They are
mounted under /api/admin/events by adminpanel/urls.py and use the admin
panel's own auth: a cookie SESSION plus IsAdmin, and JSON-only parsing.

Why JSON-only matters (the same reasoning as adminpanel._AdminWriteView):
admin writes are CSRF-exempt with a SameSite=None cookie, so protection
rests on the CORS origin allowlist. Form-encoded and multipart POSTs are
CORS-"simple" — the browser sends them cross-site with cookies and never
preflights, so the allowlist would never be consulted. Requiring JSON forces
a preflight on every write. Posters therefore go through the existing
POST /api/admin/uploads (which demands an X-Requested-With header for the
same reason) and arrive here as a URL.
"""
import logging

from django.db import transaction
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from adminpanel.auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from bookings.slots import SlotError, parse_date

from .models import Event, EventOrder
from .writes import (
    apply_core_fields,
    has_sold_tickets,
    parse_dates,
    parse_ticket_types,
    sync_dates,
    sync_ticket_types,
    unique_slug,
)

logger = logging.getLogger(__name__)

# What the panel's publish/unpublish controls map onto. "Publish" means LIVE
# (visible and bookable); "unpublish" means PAUSED rather than DRAFT, because
# an event that has already sold tickets must keep resolving for the people
# holding them — DRAFT is only ever the pre-publication state.
_STATUS_ALIASES = {
    'publish': Event.Status.LIVE,
    'published': Event.Status.LIVE,
    'live': Event.Status.LIVE,
    'approved': Event.Status.LIVE,
    'unpublish': Event.Status.PAUSED,
    'unpublished': Event.Status.PAUSED,
    'paused': Event.Status.PAUSED,
    'draft': Event.Status.DRAFT,
    # Kept so the legacy vendor-submission queue (pending / rejected) can
    # still be worked through from the same page.
    'pending': Event.Status.PENDING,
    'rejected': Event.Status.REJECTED,
}


def _event_row(event):
    """One event in the admin panel's shape (adminpanel.formatters.event_row).

    Imported lazily: adminpanel.formatters imports from events, so a
    module-level import here would close the cycle.
    """
    from adminpanel.formatters import event_row
    return event_row(event)


def _audit(request, action, event, change='', reason=None):
    from adminpanel.views import record_audit
    record_audit(
        request, action, event.name, change=change,
        target_id=str(event.pk), reason=reason,
    )


def _publishable(event):
    """(ok, message) — is this event complete enough to be sold?

    Checked on PUBLISH rather than on save, so an admin can keep a half-built
    event as a draft. Publishing an event with no bookable date or no ticket
    price would put a card on the customer's Events page that cannot be
    bought from, which is worse than refusing the publish.
    """
    from bookings.slots import today_ist

    if not event.ticket_types.filter(is_active=True).exists():
        return False, 'Add at least one active ticket type before publishing.'
    today = today_ist()
    if not event.dates.filter(date__gte=today, is_available=True).exists():
        return False, (
            'This event has no upcoming available date, so customers could '
            'not book it. Add a date of today or later before publishing.'
        )
    return True, ''


def _set_featured(event, wanted):
    """Promote / unpromote one event. Returns an error Response or None.

    Call inside transaction.atomic(). The promoted rows are LOCKED before
    they are counted, so two admins promoting at the same instant cannot
    both squeeze past the cap.
    """
    from django.utils import timezone

    if wanted:
        if event.status != Event.Status.LIVE:
            return detail(
                'Only a published event can be promoted. Publish it first.',
                status.HTTP_400_BAD_REQUEST,
            )
        promoted = list(
            Event.objects.select_for_update()
            .filter(featured=True, status=Event.Status.LIVE)
            .exclude(pk=event.pk)
            .values_list('pk', flat=True)
        )
        if len(promoted) >= Event.MAX_FEATURED:
            return Response(
                {
                    'detail': (
                        f'Up to {Event.MAX_FEATURED} events can be promoted at '
                        'once. Remove a promotion before adding another.'
                    ),
                    'code': 'PROMOTION_LIMIT',
                    'maxFeatured': Event.MAX_FEATURED,
                },
                status=status.HTTP_409_CONFLICT,
            )
    event.featured = wanted
    event.featured_at = timezone.now() if wanted else None
    event.save(update_fields=['featured', 'featured_at', 'updated_at'])
    return None


class _AdminEventView(APIView):
    """Shared auth posture for every admin event route."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [JSONParser]          # see the module docstring

    def _body(self, request):
        return request.data if isinstance(request.data, dict) else {}

    def _event(self, event_id, *, include_deleted=False):
        queryset = Event.objects.prefetch_related('dates', 'ticket_types')
        if not include_deleted:
            queryset = queryset.exclude(status=Event.Status.DELETED)
        return queryset.filter(pk=event_id).first()


class AdminEventsView(_AdminEventView):
    """
    GET  /api/admin/events — every non-deleted event
    POST /api/admin/events — create one
    """

    def get(self, request):
        from adminpanel.formatters import format_events
        return Response({'events': format_events()})

    def post(self, request):
        body = self._body(request)
        # vendor is left NULL: an admin-created event belongs to the platform.
        event = Event(created_by=request.user)

        try:
            apply_core_fields(event, body, creating=True)
            dates = parse_dates(body.get('dates'))
            ticket_types = parse_ticket_types(body.get('ticketTypes'))
        except SlotError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)

        # Status is SERVER-OWNED. The admin may ask for a draft or a straight
        # publish; anything else in the body is ignored rather than trusted.
        wants_live = str(body.get('status') or '').lower() in (
            'live', 'publish', 'published', 'approved',
        ) or body.get('publish') is True
        event.status = Event.Status.DRAFT

        with transaction.atomic():
            event.slug = unique_slug(event.name, event.pk)
            event.save()
            sync_dates(event, dates)            # no existing rows to protect
            sync_ticket_types(event, ticket_types)

        if wants_live:
            event.refresh_from_db()
            ok, message = _publishable(event)
            if not ok:
                # The event IS saved (as a draft) — losing a filled-in form
                # over a publish-readiness problem would be hostile. The
                # response says so, and carries the draft back.
                return Response(
                    {
                        'detail': message,
                        'code': 'SAVED_AS_DRAFT',
                        'event': _event_row(event),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            event.status = Event.Status.LIVE
            event.save(update_fields=['status', 'updated_at'])

        event.refresh_from_db()
        _audit(
            request, 'Created event', event,
            change=f'status: {event.status}',
        )
        logger.info('Admin %s created event %s (%s)', request.user.email, event.name, event.id)
        return Response({'event': _event_row(event)}, status=status.HTTP_201_CREATED)


class AdminEventDetailView(_AdminEventView):
    """
    GET    /api/admin/events/<id>
    PATCH  /api/admin/events/<id> — partial update, including publish state
    DELETE /api/admin/events/<id> — soft delete (refused once tickets are sold)
    """

    def get(self, request, event_id):
        event = self._event(event_id, include_deleted=True)
        if event is None:
            return detail('Event not found.', status.HTTP_404_NOT_FOUND)
        return Response({'event': _event_row(event)})

    def patch(self, request, event_id):
        event = self._event(event_id)
        if event is None:
            return detail('Event not found.', status.HTTP_404_NOT_FOUND)

        body = self._body(request)
        previous_status = event.status
        was_featured = event.featured

        try:
            apply_core_fields(event, body, creating=False)
            existing_dates = [d.date for d in event.dates.all()]
            dates = (
                parse_dates(body.get('dates'), require=False, existing=existing_dates)
                if 'dates' in body else None
            )
            ticket_types = (
                parse_ticket_types(body.get('ticketTypes'), require=False)
                if 'ticketTypes' in body else None
            )
        except SlotError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)

        wanted_status = None
        if 'status' in body:
            wanted_status = _STATUS_ALIASES.get(str(body.get('status') or '').lower())
            if wanted_status is None:
                return detail(
                    'status must be one of: draft, publish, unpublish, '
                    'pending, rejected.',
                    status.HTTP_400_BAD_REQUEST,
                )

        # Homepage promotion — independent of publishing. A strict boolean,
        # so a stray "false" string can never promote an event.
        wanted_featured = None
        if 'featured' in body:
            wanted_featured = body.get('featured')
            if not isinstance(wanted_featured, bool):
                return detail('featured must be true or false.', status.HTTP_400_BAD_REQUEST)

        if 'reviewNotes' in body:
            event.review_notes = str(body.get('reviewNotes') or '')[:2000]

        with transaction.atomic():
            if 'name' in body:
                event.slug = unique_slug(event.name, event.pk)
            event.save()
            if dates is not None:
                error = sync_dates(event, dates)
                if error:
                    transaction.set_rollback(True)
                    return detail(error, status.HTTP_409_CONFLICT)
            if ticket_types is not None:
                sync_ticket_types(event, ticket_types)

            if wanted_status is not None and wanted_status != event.status:
                event.refresh_from_db()
                if wanted_status == Event.Status.LIVE:
                    ok, message = _publishable(event)
                    if not ok:
                        # Content edits stay; only the publish is refused. The
                        # rollback keeps that honest — partially applying an
                        # edit and reporting a failure is the worst outcome.
                        transaction.set_rollback(True)
                        return detail(message, status.HTTP_400_BAD_REQUEST)
                event.status = wanted_status
                event.save(update_fields=['status', 'updated_at'])

            if wanted_featured is not None:
                event.refresh_from_db()
                if wanted_featured != event.featured:
                    error = _set_featured(event, wanted_featured)
                    if error:
                        transaction.set_rollback(True)
                        return error

        event.refresh_from_db()
        if wanted_featured is not None and event.featured != was_featured:
            _audit(
                request,
                'Promoted event' if event.featured else 'Removed event promotion',
                event,
                change=f"homepage promotion: {'on' if event.featured else 'off'}",
            )
        if wanted_status is not None and event.status != previous_status:
            _audit(
                request,
                f'Event {previous_status} → {event.status}',
                event,
                change=f'status: {previous_status} → {event.status}',
                reason=body.get('reason'),
            )
        elif set(body) - {'featured'}:
            _audit(request, 'Edited event', event, change='content updated')
        return Response({'event': _event_row(event)})

    def delete(self, request, event_id):
        event = self._event(event_id)
        if event is None:
            return detail('Event not found.', status.HTTP_404_NOT_FOUND)

        # Soft delete only, and never once money has changed hands: a paid
        # ticket must keep resolving and the sales history must stay attached
        # to the event (the same rule venue listings follow).
        if has_sold_tickets(event):
            if event.status != Event.Status.PAUSED:
                event.status = Event.Status.PAUSED
                event.save(update_fields=['status', 'updated_at'])
            _audit(
                request, 'Unpublished event (delete refused — has sales)', event,
                change='tickets already sold',
            )
            return Response(
                {
                    'detail': (
                        'This event has sold tickets, so it cannot be deleted. '
                        'It has been unpublished instead, which hides it from '
                        'customers while keeping existing tickets valid.'
                    ),
                    'code': 'HAS_SALES',
                    'event': _event_row(event),
                },
                status=status.HTTP_409_CONFLICT,
            )

        event.status = Event.Status.DELETED
        event.save(update_fields=['status', 'updated_at'])
        _audit(request, 'Deleted event', event, change='soft-deleted')
        return Response({'deleted': True, 'id': str(event_id)})


class AdminEventOrdersView(_AdminEventView):
    """GET /api/admin/events/<id>/orders?date= — who bought tickets, and
    whether they actually paid.

    Unpaid holds are INCLUDED here (unlike the vendor view, which hid them)
    with their real status, because "someone is mid-checkout" is exactly the
    kind of thing an admin chasing a payment needs to see. Totals count only
    confirmed money so the revenue figure cannot be inflated by holds.
    """

    def get(self, request, event_id):
        event = self._event(event_id, include_deleted=True)
        if event is None:
            return detail('Event not found.', status.HTTP_404_NOT_FOUND)

        # Soft-removed rows stay in the database (audit trail, and a late
        # payment.captured can still find and refund them) but leave the list.
        orders = (
            EventOrder.objects.filter(event=event).exclude(status='removed')
            .select_related('user', 'event')
        )
        raw_date = request.query_params.get('date')
        if raw_date:
            try:
                orders = orders.filter(date=parse_date(raw_date))
            except SlotError as error:
                return detail(str(error), status.HTTP_400_BAD_REQUEST)

        rows = []
        tickets = revenue = 0
        for order in orders:
            rows.append(admin_order_record(order))
            if order.status == 'confirmed':
                tickets += order.ticket_count
                revenue += order.subtotal

        return Response({
            'orders': rows,
            'total': len(rows),
            'ticketsSold': tickets,
            'revenue': revenue,
            'event': {'id': str(event.id), 'name': event.name, 'status': event.status},
        })


def admin_order_record(order):
    """One event order as the super admin sees it.

    Shared by the per-event orders list (Events → Bookings) and the
    bootstrap's `eventOrders` (the main Bookings page), so both views always
    show the same id, amount and status for the same order.
    """
    record = order.as_record()
    # The raw lifecycle value, not display_status: an admin needs to see
    # 'payment_pending' / 'refund_pending' as such, where the customer-facing
    # record derives 'completed' from the clock.
    record['paymentStatus'] = order.status
    record['paid'] = order.status in ('confirmed', 'completed')
    record['razorpayOrderId'] = order.razorpay_order_id
    record['razorpayPaymentId'] = order.razorpay_payment_id
    # Event orders are only ever paid online through Razorpay checkout; the
    # instrument (UPI/card/…) is not stored, so report 'online' only once a
    # payment is actually recorded rather than guessing.
    record['method'] = 'online' if order.razorpay_payment_id else ''
    record['refundReason'] = order.refund_reason
    record['refundAmount'] = order.refund_amount
    record['user'] = order.user.phone if order.user_id else ''
    blocked = removal_block(order)
    record['removable'] = blocked is None
    record['removeBlockedReason'] = blocked[1] if blocked else ''
    return record


def removal_block(order):
    """(code, message) when this order must NOT be removed, else None.

    The financial rule: only orders that never took money may be removed.
      * confirmed / refund_pending — money is held. Refunding is a separate,
        deliberate act; removing would erase revenue and strand the payment.
      * refunded — the money moved twice; that row IS the financial record.
      * payment_pending inside its hold — the customer may be paying right
        now, and removing would turn their payment into an auto-refund.
      * anything carrying a captured payment id — belt and braces.
    """
    if order.status in EventOrder.PAID_STATUSES:
        return ('PAID', 'This booking is paid. Paid bookings cannot be removed '
                        '— refund it first.')
    if order.status == 'refunded':
        return ('REFUNDED', 'Refunded bookings are kept as a financial record.')
    if order.status not in EventOrder.REMOVABLE_STATUSES:
        return ('NOT_REMOVABLE', 'This booking cannot be removed.')
    if order.razorpay_payment_id:
        return ('PAID', 'A payment is recorded against this booking, so it '
                        'cannot be removed.')
    if order.status == 'payment_pending' and not order.hold_expired:
        return ('IN_CHECKOUT', 'The customer may still be paying. Try again '
                               'once the 30-minute payment hold has expired.')
    return None


class AdminEventOrderDetailView(_AdminEventView):
    """DELETE /api/admin/events/<event_id>/orders/<order_id> — remove an
    UNPAID booking (failed, cancelled, or an abandoned checkout).

    A SOFT remove: the row's status becomes 'removed', so it leaves every
    list, total and seat count, yet stays in the database — the audit log
    can point at it, and a late payment.captured still finds it and is
    auto-refunded instead of being stranded. Paid and refunded orders are
    refused (see removal_block). The order must belong to the event in the
    URL, so a mistyped id can never remove some other event's booking.
    """

    def delete(self, request, event_id, order_id):
        with transaction.atomic():
            order = (
                EventOrder.objects.select_for_update()
                .filter(pk=order_id, event_id=event_id).first()
            )
            if order is None or order.status == 'removed':
                return detail('Booking not found.', status.HTTP_404_NOT_FOUND)
            blocked = removal_block(order)
            if blocked is not None:
                code, message = blocked
                return Response(
                    {'detail': message, 'code': code},
                    status=status.HTTP_409_CONFLICT,
                )
            previous = order.status
            order.status = 'removed'
            order.save(update_fields=['status'])

        from adminpanel.views import record_audit
        record_audit(
            request, 'Removed event booking',
            f'#{order.id} · {order.event_name}',
            change=f'status: {previous} → removed',
            target_id=order.id, reason=self._body(request).get('reason'),
        )
        return Response({'removed': True, 'id': order.id})
