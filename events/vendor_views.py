"""
Vendor event access — READ-ONLY.

Events are a SUPER-ADMIN product (see events/admin_views.py). The client
removed them from the vendor workflow entirely: a vendor cannot create,
edit, publish, unpublish or delete an event, and no vendor onboarding, bank
account or payout setup is involved in an event existing.

Why these routes still exist rather than being deleted outright:

  * Vendors who created events BEFORE the change still own rows, and those
    rows carry customers' paid tickets. Hiding them would leave a vendor
    unable to see who is turning up to their own ground.
  * An admin-created event may be hosted AT one of the vendor's listed
    venues. The vendor needs to know a published event is happening there.
  * A removed route 404s, which reads as "broken" to a client still on an
    older bundle. An explicit 403 with a reason reads as "not yours", which
    is the truth and is what the portal now renders.

SECURITY: every read is scoped — a vendor sees an event only if they own it
(legacy) or it is published at one of their own listings. Another vendor's
event id is a 404 and never reveals that it exists, the same posture
venues/views.get_vendor_draft takes for drafts. Every write method returns
403 regardless of ownership, so the permission does not depend on the
frontend hiding a button.
"""
import logging

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.slots import SlotError, parse_date
from venues.views import IsVendor

from .inventory import seats_sold
from .models import Event, EventOrder
from .public_views import _hhmm, date_rows

logger = logging.getLogger(__name__)

# One message, one code — the portal branches on the code, not the text.
EVENTS_ARE_ADMIN_MANAGED = (
    'Events are managed by the TheBookMyVenues team. Contact support to list, '
    'change or cancel an event.'
)
ADMIN_MANAGED_CODE = 'ADMIN_MANAGED'


def _message(text, http_status, code=''):
    body = {'message': text}
    if code:
        body['code'] = code
    return Response(body, status=http_status)


def _forbidden():
    """THE refusal for every vendor event write. Enforced server-side so it
    holds for curl, an old bundle and a hand-rolled request alike."""
    return _message(
        EVENTS_ARE_ADMIN_MANAGED, status.HTTP_403_FORBIDDEN, code=ADMIN_MANAGED_CODE
    )


def visible_events(user):
    """Events this vendor may READ.

    Their own legacy events, plus PUBLISHED events hosted at one of their
    venues. Deleted rows are excluded; drafts of other people's events are
    not the vendor's business.
    """
    return (
        Event.objects.filter(
            Q(vendor=user)
            | Q(listing__vendor=user, status=Event.Status.LIVE)
        )
        .exclude(status=Event.Status.DELETED)
        .distinct()
        .prefetch_related('dates', 'ticket_types')
    )


def get_vendor_event(request, event_id):
    """SECURITY: scoping the lookup makes a foreign event id a 404."""
    return visible_events(request.user).filter(pk=event_id).first()


def vendor_event_record(event):
    """What the vendor portal shows: the event as published, plus the
    per-date sold counts for their own door planning.

    `canEdit` is False on every row — it is sent so the portal renders the
    read-only state from the server's answer rather than from a hard-coded
    assumption that could drift from what the API actually allows.
    """
    dates = []
    for event_date in event.dates.all():
        sold = seats_sold(event_date)
        dates.append({
            'id': event_date.id,
            'date': event_date.date.isoformat(),
            'capacity': event_date.capacity,
            'sold': sold,
            'remaining': max(0, event_date.capacity - sold),
            'isAvailable': event_date.is_available,
        })
    confirmed = EventOrder.objects.filter(event=event, status='confirmed')
    return {
        'id': str(event.id),
        'slug': event.slug,
        'name': event.name,
        'description': event.description,
        'category': event.category,
        'posterUrl': event.poster_url,
        'gallery': list(event.gallery or []),
        'venueId': str(event.listing_id) if event.listing_id else None,
        'venueName': event.venue_name,
        'address': event.address,
        'locality': event.locality,
        'city': event.city,
        'mapsLink': event.maps_link,
        'contactPhone': event.contact_phone,
        'organizer': event.organizer,
        'terms': event.terms,
        'startTime': _hhmm(event.start_time),
        'endTime': _hhmm(event.end_time),
        'bookingOpensOn': (
            event.booking_opens_on.isoformat() if event.booking_opens_on else None
        ),
        'bookingClosesOn': (
            event.booking_closes_on.isoformat() if event.booking_closes_on else None
        ),
        'status': event.status,
        'reviewNotes': event.review_notes,
        'dates': dates,
        'ticketTypes': [t.as_record() for t in event.ticket_types.all()],
        'ordersCount': confirmed.count(),
        'ticketsSold': sum(o.ticket_count for o in confirmed),
        'revenue': sum(o.subtotal for o in confirmed),
        'createdAt': event.created_at.isoformat(),
        'updatedAt': event.updated_at.isoformat(),
        # Events are admin-managed — the portal shows no write controls.
        'canEdit': False,
        'managedBy': 'admin',
    }


class VendorEventsView(APIView):
    """
    GET  /api/vendors/me/events — events the vendor may see (read-only)
    POST /api/vendors/me/events — 403, events are admin-managed
    """

    permission_classes = [IsVendor]

    def get(self, request):
        events = visible_events(request.user)
        return Response({
            'events': [vendor_event_record(e) for e in events],
            'canCreate': False,
            'managedBy': 'admin',
            'notice': EVENTS_ARE_ADMIN_MANAGED,
        })

    def post(self, request):
        logger.info(
            'Vendor %s attempted to create an event — refused (admin-managed)',
            request.user.phone,
        )
        return _forbidden()


class VendorEventDetailView(APIView):
    """
    GET    /api/vendors/me/events/<id> — read-only
    PATCH  /api/vendors/me/events/<id> — 403
    DELETE /api/vendors/me/events/<id> — 403
    """

    permission_classes = [IsVendor]

    def get(self, request, event_id):
        event = get_vendor_event(request, event_id)
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)
        return Response({'event': vendor_event_record(event)})

    def patch(self, request, event_id):
        logger.info(
            'Vendor %s attempted to edit event %s — refused (admin-managed)',
            request.user.phone, event_id,
        )
        return _forbidden()

    def put(self, request, event_id):
        return _forbidden()

    def delete(self, request, event_id):
        logger.info(
            'Vendor %s attempted to delete event %s — refused (admin-managed)',
            request.user.phone, event_id,
        )
        return _forbidden()


class VendorEventPosterView(APIView):
    """POST /api/vendors/me/events/<id>/poster — 403.

    The poster is event content, and event content is the admin's. Kept as a
    route so an older vendor bundle gets the reason instead of a 404.
    """

    permission_classes = [IsVendor]

    def post(self, request, event_id):
        return _forbidden()


class VendorEventOrdersView(APIView):
    """GET /api/vendors/me/events/<id>/orders — who bought tickets.

    Scoped through get_vendor_event, so a vendor can never read another
    vendor's ticket buyers. Still allowed (and still read-only) because the
    vendor runs the door on the night.
    """

    permission_classes = [IsVendor]

    def get(self, request, event_id):
        event = get_vendor_event(request, event_id)
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)

        orders = EventOrder.objects.filter(event=event).exclude(
            status__in=('payment_pending', 'removed')
        )
        raw_date = request.query_params.get('date')
        if raw_date:
            try:
                orders = orders.filter(date=parse_date(raw_date))
            except SlotError as error:
                return _message(str(error), status.HTTP_400_BAD_REQUEST)

        rows = [o.as_record() for o in orders]
        return Response({
            'orders': rows,
            'total': len(rows),
            'dates': date_rows(event, only_sellable=False, include_counts=True),
        })
