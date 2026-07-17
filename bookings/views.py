"""
Booking endpoints (contract 1.3, 2.3, 2.4, 2.5).

The critical rule: two simultaneous requests for overlapping slots —
exactly ONE wins, the other gets 409. Enforced by locking the venue's
Listing row (select_for_update) inside a transaction, which forces
concurrent bookings for the same venue to run one after another.
Error shape everywhere: {"message": "..."}.
"""
from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.models import Listing

from .models import Booking
from .slots import (
    SlotError,
    now_minutes_ist,
    overlaps,
    parse_date,
    parse_slots,
    today_ist,
    total_minutes,
)

BOOKING_FEE = 20  # flat ₹20 per booking (contract cross-cutting rule 5)
MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _to_int(value, field):
    """Numeric form fields may arrive as strings ('120') — coerce."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise SlotError(f'{field} must be a number.')


def _booked_intervals(listing, date):
    """All (start, end) minute-intervals already booked for venue+date."""
    intervals = []
    for booking in Booking.objects.filter(listing=listing, date=date):
        try:
            intervals.extend(parse_slots(booking.slots))
        except SlotError:
            continue  # never let one bad historic row break new bookings
    return intervals


def compute_amount(listing, intervals, requested_addons):
    """
    Server-side price: round(hourly rate x minutes / 60)
    + add-ons (PRICES FROM THE LISTING, never from the client) + ₹20 fee.
    """
    rate = _to_int(listing.record.get('price') or 0, 'venue price')
    base = round(rate * total_minutes(intervals) / 60)

    catalog = {
        str(addon.get('name')): _to_int(addon.get('price') or 0, 'addon price')
        for addon in (listing.record.get('detail') or {}).get('addons') or []
    }

    addon_total = 0
    cleaned = []
    for addon in requested_addons or []:
        name = str(addon.get('name'))
        if name not in catalog:
            raise SlotError(f'Unknown add-on: "{name}".')
        qty = _to_int(addon.get('qty') or 1, 'addon qty')
        if qty < 1:
            raise SlotError('addon qty must be at least 1.')
        price = catalog[name]
        addon_total += price * qty
        cleaned.append({'name': name, 'qty': qty, 'price': price})

    return base + addon_total + BOOKING_FEE, cleaned


class AvailabilityView(APIView):
    """GET /api/venues/<id>/availability?date=YYYY-MM-DD (public).

    NOT cached on purpose — a stale "free" slot would be a lie."""

    permission_classes = [AllowAny]

    def get(self, request, listing_id):
        listing = Listing.objects.filter(
            pk=listing_id, status=Listing.Status.LIVE
        ).first()
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        try:
            date = parse_date(request.query_params.get('date'))
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        if date < today_ist():
            return _message('date cannot be in the past.', status.HTTP_400_BAD_REQUEST)

        # Every booked slot string for that day, sorted by start time.
        slot_texts = []
        for booking in Booking.objects.filter(listing=listing, date=date):
            slot_texts.extend(booking.slots)
        try:
            slot_texts.sort(key=lambda text: parse_slots([text])[0][0])
        except SlotError:
            pass

        return Response({'date': date.isoformat(), 'booked': slot_texts})


class MyBookingsView(APIView):
    """
    GET  /api/users/me/bookings — my bookings (?status=upcoming|past)
    POST /api/users/me/bookings — confirm a booking (blocks the slots)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = Booking.objects.filter(user=request.user)

        wanted = request.query_params.get('status')
        if wanted == 'upcoming':
            queryset = queryset.filter(date__gte=today_ist())
        elif wanted == 'past':
            queryset = queryset.filter(date__lt=today_ist())
        elif wanted:
            return _message('status must be "upcoming" or "past".', status.HTTP_400_BAD_REQUEST)

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
            'bookings': [row.as_record() for row in rows],
            'total': total,
        })

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        # --- Resolve the venue -------------------------------------
        listing = None
        venue_id = body.get('venueId') or body.get('listingId')
        if venue_id:
            listing = Listing.objects.filter(
                pk=str(venue_id), status=Listing.Status.LIVE
            ).first()
        elif body.get('venueName'):
            matches = list(Listing.objects.filter(
                name=str(body['venueName']), status=Listing.Status.LIVE
            )[:2])
            listing = matches[0] if len(matches) == 1 else None
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        # --- Validate date & slots ---------------------------------
        try:
            date = parse_date(body.get('date'))
            intervals = parse_slots(body.get('slots'))
            amount, addons = compute_amount(listing, intervals, body.get('addons'))
            client_amount = _to_int(body.get('amount'), 'amount')
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        today = today_ist()
        if date < today:
            return _message('Cannot book a past date.', status.HTTP_400_BAD_REQUEST)
        if date == today:
            first_start = min(start for start, _ in intervals)
            if first_start <= now_minutes_ist():
                return _message('That time has already passed today.', status.HTTP_400_BAD_REQUEST)

        # SECURITY: the client's amount is only ACCEPTED, never trusted.
        if client_amount != amount:
            return _message(
                f'Amount mismatch: expected ₹{amount}.', status.HTTP_400_BAD_REQUEST
            )

        # --- The race-safe part ------------------------------------
        with transaction.atomic():
            # Lock this venue's row: concurrent bookings for the same
            # venue now wait here and run strictly one at a time.
            Listing.objects.select_for_update().get(pk=listing.pk)

            if overlaps(intervals, _booked_intervals(listing, date)):
                return _message(
                    'One or more selected time slots were just booked. '
                    'Please pick different slots.',
                    status.HTTP_409_CONFLICT,
                )

            booking = Booking.objects.create(
                listing=listing,
                user=request.user,
                venue_name=listing.record.get('name') or listing.name,
                category=listing.category,
                location=str(listing.record.get('location') or ''),
                image=str(listing.record.get('image') or ''),
                customer_name=str(body.get('customer') or request.user.name),
                phone=request.user.phone,
                date=date,
                slots=[str(slot) for slot in body['slots']],
                per_slot=_to_int(body.get('perSlot') or listing.record.get('price') or 0, 'perSlot'),
                addons=addons,
                amount=amount,
            )

        return Response({'booking': booking.as_record()}, status=status.HTTP_201_CREATED)


class CancelBookingView(APIView):
    """DELETE /api/users/me/bookings/<id> — cancel an upcoming booking."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, booking_id):
        # Owner-only: someone else's booking id looks like it doesn't exist.
        booking = Booking.objects.filter(pk=booking_id, user=request.user).first()
        if booking is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)

        if booking.date < today_ist():
            return _message('This booking is already completed.', status.HTTP_400_BAD_REQUEST)

        booking.delete()  # the freed slots reappear in availability instantly
        return Response({'cancelled': True, 'id': booking_id})
