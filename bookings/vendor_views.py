"""
Vendor portal endpoints (contract 3.4 dashboard, 3.7 walk-in bookings).

Everything is scoped to the token owner's venues ONLY.
Error shape everywhere: {"message": "..."}.
"""
import datetime

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.models import Listing
from venues.public_views import _summary
from venues.views import IsVendor

from .models import Booking
from .slots import (
    SlotError,
    overlaps,
    parse_date,
    parse_slots,
    today_ist,
    total_minutes,
)
from .views import _booked_intervals, _message, _to_int

TODAY_BOOKINGS_LIMIT = 8


def _is_walk_in(booking):
    """Contract: walk-in = walkIn:true OR method:"walk-in"."""
    return booking['walk_in'] or booking['method'] == Booking.Method.WALK_IN


def _sum_range(rows, start, end, *, walk_in=None):
    """Sum amounts for date range [start, end]; optionally one channel."""
    total = 0
    for row in rows:
        if not (start <= row['date'] <= end):
            continue
        if walk_in is not None and _is_walk_in(row) != walk_in:
            continue
        total += row['amount']
    return total


def _trend(current, previous):
    """Percent change vs the previous period, e.g. 25 or -40."""
    if previous == 0:
        return 100 if current else 0
    return round((current - previous) / previous * 100)


class VendorDashboardView(APIView):
    """GET /api/vendors/me/dashboard — everything the dashboard shows."""

    permission_classes = [IsVendor]

    def get(self, request):
        today = today_ist()
        week_start = today - datetime.timedelta(days=6)     # rolling 7 days
        month_start = today - datetime.timedelta(days=29)   # rolling 30 days
        history_start = today - datetime.timedelta(days=59) # covers prev periods

        base = Booking.objects.filter(listing__vendor=request.user)

        # One lightweight query feeds ALL the sums below.
        rows = list(
            base.filter(date__gte=history_start)
            .values('date', 'amount', 'walk_in', 'method', 'slots')
        )

        yesterday = today - datetime.timedelta(days=1)
        prev_week_start = week_start - datetime.timedelta(days=7)
        prev_month_start = month_start - datetime.timedelta(days=30)

        today_total = _sum_range(rows, today, today)
        week_total = _sum_range(rows, week_start, today)
        month_total = _sum_range(rows, month_start, today)

        slots_today = sum(
            len(row['slots']) for row in rows if row['date'] == today
        )

        stats = {
            'today': {
                'value': today_total,
                'trend': _trend(today_total, _sum_range(rows, yesterday, yesterday)),
            },
            'slotsToday': {'value': slots_today},
            'week': {
                'value': week_total,
                'trend': _trend(
                    week_total,
                    _sum_range(rows, prev_week_start, week_start - datetime.timedelta(days=1)),
                ),
            },
            'month': {
                'value': month_total,
                'trend': _trend(
                    month_total,
                    _sum_range(rows, prev_month_start, month_start - datetime.timedelta(days=1)),
                ),
            },
        }

        earnings = {}
        for key, walk_in in (('walkIn', True), ('online', False), ('total', None)):
            earnings[key] = {
                'today': _sum_range(rows, today, today, walk_in=walk_in),
                'week': _sum_range(rows, week_start, today, walk_in=walk_in),
                'month': _sum_range(rows, month_start, today, walk_in=walk_in),
            }

        week = []
        for offset in range(6, -1, -1):
            day = today - datetime.timedelta(days=offset)
            week.append({
                'label': day.strftime('%a'),
                'value': _sum_range(rows, day, day),
                'online': _sum_range(rows, day, day, walk_in=False),
                'walkIn': _sum_range(rows, day, day, walk_in=True),
            })

        todays = base.filter(date=today).order_by('created_at')[:TODAY_BOOKINGS_LIMIT]
        today_bookings = [
            {
                'time': booking.slots[0] if booking.slots else '',
                'venue': booking.venue_name,
                'customer': booking.customer_name,
                'amount': booking.amount,
            }
            for booking in todays
        ]

        all_bookings = [
            booking.as_record()
            for booking in base.order_by('-created_at')
        ]

        venues = [
            _summary(listing) for listing in request.user.listings.all()
        ]

        return Response({
            'stats': stats,
            'earnings': earnings,
            'week': week,
            'bookings': today_bookings,
            'allBookings': all_bookings,
            'venues': venues,
        })


class WalkInBookingView(APIView):
    """
    POST /api/vendors/me/walkin-bookings — vendor blocks a time range
    for an offline customer (Availability modal).
    Same slot/overlap rules as online bookings; amount = perSlot rate x
    duration (no ₹20 fee, no add-ons).
    """

    permission_classes = [IsVendor]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        # Resolve the venue — must be one of the VENDOR'S OWN listings.
        mine = request.user.listings
        listing = None
        venue_id = body.get('venueId') or body.get('listingId')
        if venue_id:
            listing = mine.filter(pk=str(venue_id)).first()
        elif body.get('venueName'):
            matches = list(mine.filter(name=str(body['venueName']))[:2])
            listing = matches[0] if len(matches) == 1 else None
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        try:
            date = parse_date(body.get('date'))
            intervals = parse_slots(body.get('slots'))
            per_slot = _to_int(body.get('perSlot'), 'perSlot')
            client_amount = _to_int(body.get('amount'), 'amount')
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        if per_slot < 0:
            return _message('perSlot cannot be negative.', status.HTTP_400_BAD_REQUEST)
        if date < today_ist():
            return _message('Cannot book a past date.', status.HTTP_400_BAD_REQUEST)

        # Contract: walk-in amount = hourly rate x duration (no fee).
        amount = round(per_slot * total_minutes(intervals) / 60)
        if client_amount != amount:
            return _message(
                f'Amount mismatch: expected ₹{amount}.', status.HTTP_400_BAD_REQUEST
            )

        with transaction.atomic():
            Listing.objects.select_for_update().get(pk=listing.pk)

            if overlaps(intervals, _booked_intervals(listing, date)):
                return _message(
                    'One or more selected time slots are already booked.',
                    status.HTTP_409_CONFLICT,
                )

            booking = Booking.objects.create(
                listing=listing,
                user=None,  # offline customer — no account
                venue_name=listing.record.get('name') or listing.name,
                category=listing.category,
                location=str(listing.record.get('location') or ''),
                image=str(listing.record.get('image') or ''),
                customer_name=str(body.get('customer') or 'Walk-in'),
                phone='',
                date=date,
                slots=[str(slot) for slot in body['slots']],
                per_slot=per_slot,
                addons=[],
                amount=amount,
                method=Booking.Method.WALK_IN,
                walk_in=True,
            )

        return Response({'booking': booking.as_record()}, status=status.HTTP_201_CREATED)
