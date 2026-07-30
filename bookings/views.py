"""
Booking endpoints (contract 1.3, 2.3, 2.4, 2.5).

The critical rule: two simultaneous requests for overlapping slots —
exactly ONE wins, the other gets 409. Enforced by locking the venue's
Listing row (select_for_update) inside a transaction, which forces
concurrent bookings for the same venue to run one after another.
Error shape everywhere: {"message": "..."}.
"""
from collections import defaultdict

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

BOOKING_FEE = 20  # default flat ₹20 per booking (contract cross-cutting rule 5)
MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _booking_fee():
    """The live booking fee — admin-configurable via Settings, default ₹20."""
    try:
        from adminpanel.models import Settings
        return Settings.load().booking_fee
    except Exception:
        return BOOKING_FEE


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _amount_mismatch(expected):
    """Structured 400 for a wrong amount (P5a) — the frontend reads `code`
    and `expectedAmount` directly instead of regexing the message text."""
    return Response(
        {
            'message': f'Amount mismatch: expected ₹{expected}.',
            'code': 'AMOUNT_MISMATCH',
            'expectedAmount': expected,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _to_int(value, field):
    """Numeric form fields may arrive as strings ('120') — coerce."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise SlotError(f'{field} must be a number.')


def _booked_intervals(listing, date, sport=None, unit=None):
    """
    (start, end) minute-intervals already booked for venue+date that CONFLICT
    with a booking for (sport, unit).

    - unit=None (whole venue / single-unit) conflicts with every booking —
      the original behaviour.
    - unit set: conflicts only with the SAME (sport, unit), plus legacy /
      whole-venue bookings (unit is None) which block every unit (safe default).
    """
    intervals = []
    for booking in Booking.objects.filter(listing=listing, date=date):
        if unit is not None and booking.unit is not None:
            if booking.unit != unit or (booking.sport or '') != (sport or ''):
                continue  # a different pitch/court/screen — no conflict
        try:
            intervals.extend(parse_slots(booking.slots))
        except SlotError:
            continue  # never let one bad historic row break new bookings
    return intervals


def _parse_unit(body):
    """
    (sport, unit, unitLabel) from a booking request.
    unit is a 1-based int, or None for a whole-venue / single-unit booking.
    """
    sport = str(body.get('sport') or '').strip()
    raw = body.get('unit')
    if raw in (None, ''):
        unit = None
    else:
        unit = _to_int(raw, 'unit')
        if unit < 1:
            raise SlotError('unit must be 1 or higher.')
    unit_label = str(body.get('unitLabel') or '').strip()
    return sport, unit, unit_label


def _unit_rate(listing, sport, unit):
    """
    The listing's hourly rate for a specific (sport, unit), or None when the
    venue isn't priced per unit (caller then falls back to the listing price).

      playzone:     detail.sports[name==sport].unitPrices[unit-1]
      hall/theatre: detail.unitPrices[unit-1]
    """
    if unit is None:
        return None
    detail = listing.record.get('detail') or {}
    if sport:
        for entry in detail.get('sports') or []:
            if str(entry.get('name')) == sport:
                prices = entry.get('unitPrices') or []
                if 1 <= unit <= len(prices) and str(prices[unit - 1]).strip():
                    return _to_int(prices[unit - 1], 'unit price')
                if str(entry.get('price') or '').strip():
                    return _to_int(entry.get('price'), 'sport price')
                return None
        return None
    prices = detail.get('unitPrices') or []
    if 1 <= unit <= len(prices) and str(prices[unit - 1]).strip():
        return _to_int(prices[unit - 1], 'unit price')
    return None


def compute_amount(listing, intervals, requested_addons, rate=None):
    """
    Server-side price:
        round(hourly rate x minutes / 60)   (slot — per-unit rate if given,
                                             else the listing price)
      + sum(addon.price x qty)              (add-ons — priced FROM THE REQUEST)
      + ₹20 fee

    The frontend folds packages and extra-person charges INTO the `addons`
    array as ordinary priced line items (names that deliberately are NOT in the
    listing's add-on catalogue). We therefore take each add-on's price from the
    request and never reject a line for having an unrecognised name — otherwise
    packages / extra persons / vendor-custom add-ons would fail every booking.

    NOTE: this trusts client-supplied add-on prices. That is acceptable while
    there is no live payment (amount is only a recorded number today); revisit
    and re-validate against the catalogue when real payments land.
    """
    if rate is None:
        rate = _to_int(listing.record.get('price') or 0, 'venue price')
    base = round(rate * total_minutes(intervals) / 60)

    addon_total = 0
    cleaned = []
    for addon in requested_addons or []:
        name = str(addon.get('name') or '').strip()
        if not name:
            raise SlotError('Each add-on needs a name.')
        qty = _to_int(addon.get('qty') or 1, 'addon qty')
        if qty < 1:
            raise SlotError('addon qty must be at least 1.')
        price = _to_int(addon.get('price') or 0, 'addon price')
        if price < 0:
            raise SlotError('addon price cannot be negative.')
        addon_total += price * qty
        cleaned.append({'name': name, 'qty': qty, 'price': price})

    return base + addon_total + _booking_fee(), cleaned


def _slot_start(text):
    try:
        return parse_slots([text])[0][0]
    except SlotError:
        return 0


def _total_units(listing):
    """How many bookable units the venue has (pitches/courts/screens)."""
    detail = listing.record.get('detail') or {}
    sports_units = sum(int(s.get('units') or 1) for s in (detail.get('sports') or []))
    return sports_units or len(detail.get('unitPrices') or []) or 1


def _availability(listing, date):
    """
    Availability payload (P6). Keeps the flat `booked` array (ranges taken on
    EVERY unit, so single-unit clients still work) and adds per-unit `bookedUnits`.
    """
    total_units = _total_units(listing)
    legacy = []                          # whole-venue bookings block every unit
    per_unit = defaultdict(list)         # (sport, unit) -> [slot strings]
    slot_units = defaultdict(set)        # slot string -> {(sport, unit), ...}

    for booking in Booking.objects.filter(listing=listing, date=date):
        if booking.unit is None:
            legacy.extend(booking.slots)
        else:
            key = (booking.sport or '', booking.unit)
            for slot in booking.slots:
                per_unit[key].append(slot)
                slot_units[slot].add(key)

    booked = set(legacy)
    for slot, units in slot_units.items():
        if len(units) >= total_units:   # taken on every unit -> fully booked
            booked.add(slot)

    booked_units = [
        {'sport': sport or None, 'unit': unit, 'ranges': sorted(ranges, key=_slot_start)}
        for (sport, unit), ranges in sorted(per_unit.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]

    return {
        'date': date.isoformat(),
        'booked': sorted(booked, key=_slot_start),
        'bookedUnits': booked_units,
    }


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

        return Response(_availability(listing, date))


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

        # --- Validate date, slots, unit & amount -------------------
        try:
            date = parse_date(body.get('date'))
            intervals = parse_slots(body.get('slots'))
            sport, unit, unit_label = _parse_unit(body)
            rate = _unit_rate(listing, sport, unit)  # None -> listing price
            amount, addons = compute_amount(listing, intervals, body.get('addons'), rate=rate)
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
            return _amount_mismatch(amount)

        # --- The race-safe part ------------------------------------
        with transaction.atomic():
            # Lock this venue's row: concurrent bookings for the same
            # venue now wait here and run strictly one at a time.
            Listing.objects.select_for_update().get(pk=listing.pk)

            if overlaps(intervals, _booked_intervals(listing, date, sport, unit)):
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
                sport=sport,
                unit=unit,
                unit_label=unit_label,
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
