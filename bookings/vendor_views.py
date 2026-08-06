"""
Vendor portal endpoints (contract 3.4 dashboard, 3.7 walk-in bookings).

Everything is scoped to the token owner's venues ONLY.
Error shape everywhere: {"message": "..."}.
"""
import datetime

from django.db import transaction
from django.utils import timezone
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
from .views import (
    _amount_mismatch,
    _apply_offer,
    _booked_intervals,
    _message,
    _parse_unit,
    _to_int,
    exclude_dead,
)

TODAY_BOOKINGS_LIMIT = 8


def _is_walk_in(booking):
    """Contract: walk-in = walkIn:true OR method:"walk-in"."""
    return booking['walk_in'] or booking['method'] == Booking.Method.WALK_IN


def booking_net(row):
    """What the VENDOR earns from one booking: amount + platform-promo
    top-up − the booking's frozen fee. One formula for every channel (see
    _all_time_earnings). `row` is a values() dict or anything dict-like."""
    promo = (
        row['discount_amount']
        if (row['offer'] or {}).get('source') == 'platform' else 0
    )
    return max(0, row['amount'] + promo - row['fee'])


def _sum_range(rows, start, end, *, walk_in=None):
    """Sum NET earnings for date range [start, end]; optionally one channel.
    Net, not gross, so every figure on the dashboard answers the same
    question as stats.total."""
    total = 0
    for row in rows:
        if not (start <= row['date'] <= end):
            continue
        if walk_in is not None and _is_walk_in(row) != walk_in:
            continue
        total += row['net']
    return total


def _trend(current, previous):
    """Percent change vs the previous period, e.g. 25 or -40."""
    if previous == 0:
        return 100 if current else 0
    return round((current - previous) / previous * 100)


def _all_time_earnings(base):
    """
    {value, online, walkIn, count} — what the vendor has actually EARNED over
    all time, using the same per-booking rules as the payout math:

      net = amount + platform-promo top-up − the booking's frozen fee

    Every channel uses that one formula: an online customer pays `amount` and
    we keep `fee`; a pay-at-venue customer hands the vendor `amount` in cash
    and the vendor owes us `fee`; a walk-in carries fee=0, so it is the full
    amount. Platform promos are OUR marketing, so the vendor is made whole
    (+ discountAmount) — venue-funded offers are not.

    NOTE: this is EARNINGS, not the payout transfer. A pay-at-venue booking
    earns amount − fee but transfers −fee, because the vendor already holds
    the cash (see adminpanel/payouts.py).

    Excludes cancelled / refunded / refund_pending / payment_pending — money
    that was never kept.
    """
    value = online = walk_in_total = count = 0
    rows = base.exclude(status='refund_pending').values(
        'amount', 'fee', 'discount_amount', 'offer', 'walk_in', 'method',
    )
    for row in rows:
        net = booking_net(row)
        value += net
        count += 1
        if row['walk_in'] or row['method'] == Booking.Method.WALK_IN:
            walk_in_total += net
        else:
            online += net
    return {'value': value, 'online': online, 'walkIn': walk_in_total, 'count': count}


class VendorDashboardView(APIView):
    """GET /api/vendors/me/dashboard — everything the dashboard shows."""

    permission_classes = [IsVendor]

    def get(self, request):
        today = today_ist()
        week_start = today - datetime.timedelta(days=6)     # rolling 7 days
        month_start = today - datetime.timedelta(days=29)   # rolling 30 days
        history_start = today - datetime.timedelta(days=59) # covers prev periods

        # Same exclusion the availability endpoint applies when it frees a
        # slot (cancelled/abandoned + refunded), plus unpaid holds — a hold is
        # not a booking until it is paid. Keeps stats, the week chart,
        # earnings and the booking lists consistent with the slot calendar.
        base = exclude_dead(
            Booking.objects.filter(listing__vendor=request.user)
        ).exclude(status='payment_pending')

        # One lightweight query feeds ALL the sums below.
        rows = list(
            base.filter(date__gte=history_start).values(
                'date', 'amount', 'fee', 'discount_amount', 'offer',
                'walk_in', 'method', 'slots',
            )
        )
        for row in rows:
            row['net'] = booking_net(row)

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
            # All-time net earnings. today/week/month above use the SAME
            # net-per-booking rule, just windowed — every figure on this
            # dashboard answers the same question.
            'total': _all_time_earnings(base),
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

        from .ratings import venue_ratings_map
        own_listings = list(
            request.user.listings.exclude(status=Listing.Status.DELETED)
        )
        venue_ratings = venue_ratings_map(own_listings)
        venues = [_summary(listing, venue_ratings) for listing in own_listings]

        return Response({
            'stats': stats,
            'earnings': earnings,
            'week': week,
            'bookings': today_bookings,
            'allBookings': all_bookings,
            'venues': venues,
        })


class CollectBookingView(APIView):
    """
    POST /api/vendors/me/bookings/<id>/collect — the vendor ticks "cash
    collected" on a pay-at-venue / walk-in booking. Owner-only, idempotent.

    DISPLAY ONLY: amounts, stats, earnings and payout math are untouched.
    Send {"collected": false} to undo a mis-tap.
    """

    permission_classes = [IsVendor]

    def post(self, request, booking_id):
        booking = Booking.objects.filter(pk=booking_id).first()
        if booking is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)
        if booking.listing is None or booking.listing.vendor_id != request.user.id:
            return _message(
                'This booking is not for one of your venues.',
                status.HTTP_403_FORBIDDEN,
            )

        body = request.data if isinstance(request.data, dict) else {}
        collected = body.get('collected', True) is not False

        if collected != booking.collected:
            booking.collected = collected
            booking.collected_at = timezone.now() if collected else None
            booking.save(update_fields=['collected', 'collected_at'])

        return Response({'booking': booking.as_record()})


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
        if listing.status != Listing.Status.LIVE:
            return _message(
                'This venue is not live yet — walk-ins need an approved venue.',
                status.HTTP_400_BAD_REQUEST,
            )

        try:
            date = parse_date(body.get('date'))
            intervals = parse_slots(body.get('slots'))
            sport, unit, unit_label = _parse_unit(body)
            per_slot = _to_int(body.get('perSlot'), 'perSlot')
            client_amount = _to_int(body.get('amount'), 'amount')
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        if per_slot < 0:
            return _message('perSlot cannot be negative.', status.HTTP_400_BAD_REQUEST)
        if date < today_ist():
            return _message('Cannot book a past date.', status.HTTP_400_BAD_REQUEST)

        # Contract: walk-in amount = hourly rate x duration, minus any coupon,
        # NO ₹20 fee.
        base = round(per_slot * total_minutes(intervals) / 60)
        try:
            discount, applied_offer = _apply_offer(listing, base, body.get('offer'))
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)
        amount = max(0, base - discount)
        if client_amount != amount:
            return _amount_mismatch(amount)

        with transaction.atomic():
            Listing.objects.select_for_update().get(pk=listing.pk)

            if overlaps(intervals, _booked_intervals(listing, date, sport, unit)):
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
                sport=sport,
                unit=unit,
                unit_label=unit_label,
                date=date,
                slots=[str(slot) for slot in body['slots']],
                per_slot=per_slot,
                addons=[],
                offer=applied_offer,
                discount_amount=discount,
                fee=0,  # walk-ins carry no platform fee
                amount=amount,
                method=Booking.Method.WALK_IN,
                walk_in=True,
            )

        return Response({'booking': booking.as_record()}, status=status.HTTP_201_CREATED)
