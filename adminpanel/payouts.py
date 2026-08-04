"""
Weekly payout generation.

Business model: the platform's ONLY revenue is the flat booking fee — no
percentage commission. Per completed week, a vendor's payout is:

    Σ online-paid (amount − fee)   we hold the money, pass it on minus our fee
  − Σ pay-at-venue (fee)           vendor collected the FULL amount (incl. our
                                   fee) in cash — only the fee comes back to us

Each booking's own frozen fee is used (set at creation), so later fee changes
never corrupt old periods. Walk-ins are excluded entirely (vendor cash, fee=0);
refunded/cancelled bookings are excluded.

generate_payouts() is idempotent (one row per vendor per week, enforced by a DB
constraint) and runs on every admin bootstrap, so the Payouts page fills itself.
"""
import datetime

from bookings.models import Booking
from bookings.slots import today_ist

from .models import Payout


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
    the past). Existing rows are never touched — admins may have processed
    them. A week that nets NEGATIVE (at-venue fees owed exceed the online
    payout) creates no row; the deficit carries forward and is deducted from
    the vendor's next positive week."""
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
    carry = {}  # vendor id -> negative balance carried into the next week
    while week < current_week:
        week_end = week + datetime.timedelta(days=6)
        totals = {}  # vendor user -> [net, name]
        for booking in payable.filter(date__range=(week, week_end)):
            vendor = booking.listing.vendor
            entry = totals.setdefault(vendor.id, [0, vendor.name or vendor.phone])
            # PLATFORM promos are our marketing: the vendor is paid as if no
            # discount existed (the platform funds it). Venue offers are
            # vendor-funded — no top-up.
            promo_topup = (
                booking.discount_amount
                if (booking.offer or {}).get('source') == 'platform'
                else 0
            )
            if booking.method == Booking.Method.VENUE:
                # Vendor collected the DISCOUNTED cash; we top up the platform
                # promo and claw back our fee.
                entry[0] += promo_topup - booking.fee
            else:
                entry[0] += max(0, booking.amount + promo_topup - booking.fee)

        for vendor_id, (net, name) in totals.items():
            if Payout.objects.filter(
                vendor_user_id=vendor_id, period_start=week
            ).exists():
                continue  # already settled — never touch, never re-carry
            total = net + carry.get(vendor_id, 0)
            if total <= 0:
                carry[vendor_id] = total  # debt rolls into the next week
                continue
            Payout.objects.create(
                vendor_user_id=vendor_id,
                vendor=name,
                period=_period_label(week, week_end),
                period_start=week,
                period_end=week_end,
                gross=total,
                status=Payout.Status.PENDING,
            )
            carry[vendor_id] = 0
        week += datetime.timedelta(days=7)
