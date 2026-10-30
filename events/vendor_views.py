"""
Vendor events — vendors SUBMIT events; a super admin approves them.

    GET    /api/vendors/me/events                 events this vendor may see
    POST   /api/vendors/me/events                 submit a new event → PENDING
    GET    /api/vendors/me/events/<id>
    PATCH  /api/vendors/me/events/<id>            edit OWN event → back to PENDING
    DELETE /api/vendors/me/events/<id>            withdraw OWN event (no sales only)
    POST   /api/vendors/me/event-images           upload a poster/gallery image → {url}
    POST   /api/vendors/me/events/<id>/poster     same upload, for an existing own event
    GET    /api/vendors/me/events/<id>/orders     who bought tickets (unchanged)

The approval lifecycle reuses the statuses events already had:

    vendor submits ─► PENDING ──admin approves──► LIVE (public, bookable)
                         │                         │
                         └──admin rejects──► REJECTED (reason in review_notes)
    any vendor edit of their event (pending / rejected / live / paused)
                    ─► PENDING again — an approved event can never be changed
                       into something the admin did not approve.

STATUS IS SERVER-OWNED: a vendor can never set status, featured, organiser or
review fields, and the owner is the token's account — any vendor / owner id in
the body is ignored. Only the admin endpoint (events/admin_views.py, session +
IsAdmin) can approve, publish or promote. Public listing, detail and checkout
already serve LIVE events only (events/public_views.live_events and
Event.BOOKABLE_STATUSES), so a pending or rejected event is invisible and
unbookable however it is addressed.

Field parsing and the rules that protect existing ticket holders (a date with
sales cannot be removed or shrunk; a sold ticket type is deactivated, never
deleted) are the SAME code the admin uses — events/writes.py.

SECURITY: reads are scoped (own events, plus published events hosted at the
vendor's own venues — visible_events); writes require OWNERSHIP (event.vendor
is the caller). Another vendor's event is a 404 and an admin event at the
vendor's venue is a 403.
"""
import logging
import uuid

from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.slots import SlotError, parse_date
from venues.storage import ALLOWED_TYPES, MAX_FILE_SIZE, StorageError, upload_photo
from venues.views import IsVendor, _looks_like_image

from .inventory import seats_sold
from .models import Event, EventOrder
from .public_views import _hhmm, date_rows
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

# Keys a vendor may never set — status / promotion / review are the admin's,
# the organiser is the vendor's own account, and ownership comes from the token.
ADMIN_ONLY_KEYS = (
    'status', 'publish', 'featured', 'reviewNotes', 'organizerName', 'organizerPhone',
    'vendor', 'vendorId', 'createdBy', 'created_by', 'id', 'slug',
)
SUBMITTED_NOTICE = (
    'Events you submit are reviewed by the TheBookMyVenues team before they go '
    'live. Editing a live event sends it back for review.'
)
NOT_YOUR_EVENT = 'Only the vendor who submitted this event can change it.'


def _message(text, http_status, code=''):
    body = {'message': text}
    if code:
        body['code'] = code
    return Response(body, status=http_status)


def _vendor_body(request):
    """The request body minus every field a vendor is not allowed to set."""
    body = dict(request.data) if isinstance(request.data, dict) else {}
    for key in ADMIN_ONLY_KEYS:
        body.pop(key, None)
    return body


def _check_image_urls(body):
    """Vendor-supplied image URLs must be plain http(s) — never javascript:/
    data: — because the customer site renders them as image sources."""
    urls = [body.get('posterUrl'), body.get('poster')]
    gallery = body.get('gallery')
    if isinstance(gallery, list):
        urls += gallery
    for url in urls:
        text = str(url or '').strip()
        if text and not text.lower().startswith(('https://', 'http://')):
            raise SlotError('Images must be web addresses starting with https://.')


def visible_events(user):
    """Events this vendor may READ.

    Their own events (any state except deleted — including pending and
    rejected, so they can follow the review), plus PUBLISHED events hosted at
    one of their venues.
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


def _owned(request, event_id):
    """(event, error Response) — the caller's OWN event, for writes."""
    event = get_vendor_event(request, event_id)
    if event is None:
        return None, _message('Event not found.', status.HTTP_404_NOT_FOUND)
    if event.vendor_id != request.user.id:
        return None, _message(NOT_YOUR_EVENT, status.HTTP_403_FORBIDDEN, code='NOT_OWNER')
    return event, None


def sales_status(event, dates, today=None):
    """Where ticket sales stand, in the order a vendor would ask:

      not_live      not approved/published (pending, rejected, paused…)
      ended         every date is in the past
      not_open_yet  the selling window opens later
      closed        the selling window has closed
      sold_out      no seats left on any upcoming open date
      on_sale       otherwise

    Read-only: the checkout applies the same rules itself
    (inventory.date_is_sellable)."""
    from bookings.slots import today_ist

    today = today or today_ist()
    if event.status not in Event.BOOKABLE_STATUSES:
        return 'not_live'
    upcoming = [d for d in dates if d['date'] >= today.isoformat()]
    if not upcoming:
        return 'ended'
    if event.booking_opens_on and today < event.booking_opens_on:
        return 'not_open_yet'
    if event.booking_closes_on and today > event.booking_closes_on:
        return 'closed'
    if not any(d['isAvailable'] and (d['remaining'] is None or d['remaining'] > 0) for d in upcoming):
        return 'sold_out'
    return 'on_sale'


def vendor_event_record(event, user=None):
    """What the vendor portal shows: the event, its review state, and the
    per-date sold counts for their own door planning. `canEdit` is the
    SERVER's answer (own, non-deleted event), so the portal never offers a
    control the API would refuse."""
    dates = []
    for event_date in event.dates.all():
        sold = seats_sold(event_date)
        dates.append({
            'id': event_date.id,
            'date': event_date.date.isoformat(),
            'capacity': event_date.capacity,
            'unlimited': event_date.capacity is None,
            'sold': sold,
            'remaining': None if event_date.capacity is None else max(0, event_date.capacity - sold),
            'isAvailable': event_date.is_available,
        })
    confirmed = list(EventOrder.objects.filter(event=event, status='confirmed'))
    own = bool(user is not None and event.vendor_id == user.id)
    # Tickets sold per ticket type (orders freeze each line's type key).
    sold_by_type = {}
    for order in confirmed:
        for item in order.items or []:
            if isinstance(item, dict):
                key = str(item.get('key') or item.get('label') or '')
                sold_by_type[key] = sold_by_type.get(key, 0) + int(item.get('qty') or 0)
    ticket_types = []
    for ticket_type in event.ticket_types.all():
        row = ticket_type.as_record()
        row['sold'] = sold_by_type.get(str(ticket_type.key), 0)
        ticket_types.append(row)
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
        # The admin's rejection reason, shown to the vendor on a rejected event.
        'reviewNotes': event.review_notes,
        'dates': dates,
        'ticketTypes': ticket_types,
        'salesStatus': sales_status(event, dates),
        'ordersCount': len(confirmed),
        'ticketsSold': sum(o.ticket_count for o in confirmed),
        'revenue': sum(o.subtotal for o in confirmed),
        'createdAt': event.created_at.isoformat(),
        'updatedAt': event.updated_at.isoformat(),
        'canEdit': own and event.status != Event.Status.DELETED,
        'managedBy': 'vendor' if event.vendor_id else 'admin',
    }


class VendorEventsView(APIView):
    """GET the vendor's events; POST submits a new one for approval."""

    permission_classes = [IsVendor]

    def get(self, request):
        events = visible_events(request.user)
        return Response({
            'events': [vendor_event_record(e, request.user) for e in events],
            'canCreate': True,
            'managedBy': 'vendor',
            'notice': SUBMITTED_NOTICE,
        })

    def post(self, request):
        body = _vendor_body(request)
        # Owner = the token's account. Never the body's.
        event = Event(vendor=request.user, created_by=request.user)
        try:
            _check_image_urls(body)
            apply_core_fields(event, body, creating=True, listing_owner=request.user)
            dates = parse_dates(body.get('dates'))
            ticket_types = parse_ticket_types(body.get('ticketTypes'))
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        event.status = Event.Status.PENDING      # server-owned: awaits approval
        with transaction.atomic():
            event.slug = unique_slug(event.name, event.pk)
            event.save()
            sync_dates(event, dates)
            sync_ticket_types(event, ticket_types)

        event.refresh_from_db()
        logger.info('Vendor %s submitted event %s (%s) for approval',
                    request.user.pk, event.name, event.pk)
        return Response(
            {'event': vendor_event_record(event, request.user)},
            status=status.HTTP_201_CREATED,
        )


class VendorEventDetailView(APIView):
    """GET one event; PATCH / DELETE the vendor's OWN event."""

    permission_classes = [IsVendor]

    def get(self, request, event_id):
        event = get_vendor_event(request, event_id)
        if event is None:
            return _message('Event not found.', status.HTTP_404_NOT_FOUND)
        return Response({'event': vendor_event_record(event, request.user)})

    def patch(self, request, event_id):
        event, error = _owned(request, event_id)
        if error is not None:
            return error
        body = _vendor_body(request)
        if not body:
            return Response({'event': vendor_event_record(event, request.user)})
        try:
            _check_image_urls(body)
            apply_core_fields(event, body, creating=False, listing_owner=request.user)
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
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        previous = event.status
        with transaction.atomic():
            if 'name' in body:
                event.slug = unique_slug(event.name, event.pk)
            # Any change goes back through review — an approved event can never
            # be edited into something the admin did not approve. Tickets
            # already sold stay valid (orders never depend on the event being
            # LIVE); new sales pause until the admin approves again.
            event.status = Event.Status.PENDING
            event.review_notes = ''
            event.save()                    # Event.save drops any promotion
            if dates is not None:
                date_error = sync_dates(event, dates)
                if date_error:
                    transaction.set_rollback(True)
                    return _message(date_error, status.HTTP_409_CONFLICT)
            if ticket_types is not None:
                sync_ticket_types(event, ticket_types)

        event.refresh_from_db()
        logger.info('Vendor %s edited event %s: %s → pending review',
                    request.user.pk, event.pk, previous)
        return Response({
            'event': vendor_event_record(event, request.user),
            'resubmitted': previous != Event.Status.PENDING,
        })

    def put(self, request, event_id):
        return self.patch(request, event_id)

    def delete(self, request, event_id):
        event, error = _owned(request, event_id)
        if error is not None:
            return error
        # Tickets that were paid for must keep resolving and the sales history
        # must stay attached — the same rule the admin delete follows.
        if has_sold_tickets(event):
            return _message(
                'This event has sold tickets, so it cannot be deleted. Contact '
                'the TheBookMyVenues team to cancel it.',
                status.HTTP_409_CONFLICT, code='HAS_SALES',
            )
        event.status = Event.Status.DELETED
        event.save(update_fields=['status', 'updated_at'])
        logger.info('Vendor %s withdrew event %s', request.user.pk, event.pk)
        return Response({'deleted': True, 'id': str(event.pk)})


def _upload_event_image(request, folder):
    """Store one image and return {url} — the same checks venue photos get:
    type allowlist, size cap, and a real image signature (not just a label)."""
    file = request.FILES.get('file')
    if file is None:
        return _message('No file uploaded.', status.HTTP_400_BAD_REQUEST)
    if file.content_type not in ALLOWED_TYPES:
        return _message('Only JPEG, PNG or WebP images are allowed.', status.HTTP_400_BAD_REQUEST)
    if file.size > MAX_FILE_SIZE:
        return _message('Image is too large (max 5 MB).', status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    if not _looks_like_image(file):
        return _message('That file is not a valid JPEG, PNG or WebP image.', status.HTTP_400_BAD_REQUEST)
    # Pixel checks (venues/image_specs.py, "poster" rule): too small or
    # absurdly large is refused; shape is advice only.
    from venues.image_specs import listing_image_check
    data = file.read()
    problem, image_info = listing_image_check(data, 'poster')
    if problem:
        return _message(problem, status.HTTP_400_BAD_REQUEST)
    path = f'events/{folder}/{uuid.uuid4().hex[:12]}.{ALLOWED_TYPES[file.content_type]}'
    try:
        url = upload_photo(path, data, file.content_type)
    except StorageError:
        logger.exception('Event image upload failed for vendor %s', request.user.pk)
        return _message('Could not store the image right now. Please try again.',
                        status.HTTP_502_BAD_GATEWAY)
    return Response({'url': url, 'image': image_info}, status=status.HTTP_201_CREATED)


class VendorEventImageUploadView(APIView):
    """POST /api/vendors/me/event-images (multipart `file`) → {url}. The URL
    is then sent as posterUrl / gallery on create or edit — so an image only
    reaches customers after the admin approves the event carrying it."""

    permission_classes = [IsVendor]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        return _upload_event_image(request, f'vendor-{request.user.pk}')


class VendorEventPosterView(APIView):
    """POST /api/vendors/me/events/<id>/poster — the same upload for one of
    the vendor's own events. Returns {url}; the event changes only when the
    vendor saves it (which sends it for review)."""

    permission_classes = [IsVendor]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, event_id):
        event, error = _owned(request, event_id)
        if error is not None:
            return error
        return _upload_event_image(request, str(event.pk))


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
