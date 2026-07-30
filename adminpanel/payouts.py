"""
Weekly payout generation.

Business model: the platform's ONLY revenue is the flat ₹20 booking fee — no
percentage commission. The customer pays slots + add-ons + ₹20; the vendor's
payout is therefore Σ(booking.amount − ₹20) over their completed ONLINE
bookings in the week. Walk-ins are excluded (the vendor already has that cash);
refunded/cancelled bookings are excluded.

generate_payouts() is idempotent (one row per vendor per week, enforced by a DB
constraint) and runs on every admin bootstrap, so the Payouts page fills itself.
"""
import datetime

from bookings.models import Booking
from bookings.slots import today_ist

from .models import Payout

PLATFORM_FEE = 20  # ₹ per online booking (the business model's constant)


def _week_start(day):
    """Monday of the week containing `day`."""
    return day - datetime.timedelta(days=day.weekday())


def _period_label(start, end):
    """'14–20 Jul' (same month) or '28 Jul – 3 Aug'."""
    if start.month == end.month:
        return f"{start.day}–{end.day} {end.strftime('%b')}"
    return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b')}"


def generate_payouts():
    """Create missing payout rows for every COMPLETED week (Mon–Sun fully in
    the past). Existing rows are never touched — admins may have processed them."""
    today = today_ist()
    current_week = _week_start(today)

    payable = (
        Booking.objects.filter(walk_in=False, date__lt=current_week)
        .exclude(status__in=['refunded', 'cancelled'])
        .exclude(listing__isnull=True)
        .select_related('listing__vendor')
    )
    first = payable.order_by('date').first()
    if first is None:
        return

    week = _week_start(first.date)
    while week < current_week:
        week_end = week + datetime.timedelta(days=6)
        totals = {}  # vendor user -> [gross, name]
        for booking in payable.filter(date__range=(week, week_end)):
            vendor = booking.listing.vendor
            entry = totals.setdefault(vendor.id, [0, vendor.name or vendor.phone])
            entry[0] += max(0, booking.amount - PLATFORM_FEE)

        for vendor_id, (gross, name) in totals.items():
            if gross <= 0:
                continue
            Payout.objects.get_or_create(
                vendor_user_id=vendor_id,
                period_start=week,
                defaults={
                    'vendor': name,
                    'period': _period_label(week, week_end),
                    'period_end': week_end,
                    'gross': gross,
                    'status': Payout.Status.PENDING,
                },
            )
        week += datetime.timedelta(days=7)
