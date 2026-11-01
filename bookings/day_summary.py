"""
Per-day availability summary for the customer's horizontal date strip.

    GET /api/venues/<id>/availability/days?from=YYYY-MM-DD&days=7   (public)

One row per day: how many one-hour start times are still bookable out of
the ones the venue offers that day, and a coarse demand band derived from
that ratio. Read-only — it never reserves anything, and booking still goes
through the full per-date check (AvailabilityView + validate_booking_request
under the listing lock). The strip is a hint; the server stays the
authority.

Every input is the SAME source the booking check reads, so the strip can
never call a day free that the checkout would refuse:
  * bookings   bookings.views._active_bookings (live holds count as taken,
               expired holds and dead rows do not; the signed-in viewer's
               OWN live hold is left out, exactly as in _availability)
  * hours      venues.business.open_periods (weekly hours, special dates,
               closures, Venue Timings default)
  * blocks     venues.business.blocks_for (vendor blocks for this pitch)
  * today      start times at or before the IST clock are gone (the create
               check refuses `first_start <= now`)

The grid is the customer picker's: one-hour starts on the hour, 06:00 to
23:00 (frontend utils/slots STEP_MIN / OPEN_MIN / CLOSE_MIN).
"""
import datetime

from .slots import CLOSE_MINUTE, OPEN_MINUTE, SlotError, now_minutes_ist, parse_slot, today_ist
from .demand import demand_band

GRID_MINUTES = 60            # the customer picker's start-time step
MAX_DAYS = 14                # one request covers at most two weeks
MAX_AHEAD_DAYS = 366         # nothing is bookable further out than a year


def _taken_intervals(listing, day, viewer=None):
    from .views import _active_bookings

    out = []
    for booking in _active_bookings(listing, day):
        if (booking.status == 'payment_pending' and viewer is not None
                and booking.user_id == viewer.pk):
            continue
        for text in booking.slots or []:
            try:
                out.append(parse_slot(text))
            except SlotError:
                continue        # a malformed legacy label never hides a day
    return out


def _hits(start, end, intervals):
    return any(start < e and s < end for s, e in intervals)


def day_row(listing, day, *, settings=None, base=None, viewer=None, today=None, now_minute=None):
    """The summary for one day. `settings`/`base` may be passed in so a
    multi-day request reads them once."""
    from venues.business import base_listing, blocks_for, open_periods

    base = base or base_listing(listing)
    periods, why = open_periods(settings, day, listing=base)
    row = {'date': day.isoformat(), 'closed': not periods}
    if not periods:
        note = (why or {}).get('note') if (why or {}).get('kind') == 'closure' else None
        row.update({'status': 'closed', 'reason': note or None,
                    'freeSlots': 0, 'totalSlots': 0})
        return row

    blocked = [(b.start_minute, b.end_minute) for b in blocks_for(listing, day, base)]
    taken = _taken_intervals(listing, day, viewer=viewer)
    today = today or today_ist()
    cutoff = (now_minute if now_minute is not None else now_minutes_ist()) if day == today else -1

    total = free = 0
    for start in range(OPEN_MINUTE, CLOSE_MINUTE, GRID_MINUTES):
        end = start + GRID_MINUTES
        if not any(ps <= start and end <= pe for ps, pe in periods):
            continue                    # outside opening hours
        if start <= cutoff or _hits(start, end, blocked):
            continue                    # gone for today / vendor block
        total += 1
        if not _hits(start, end, taken):
            free += 1

    if total == 0:
        # Open today but every remaining hour is past or blocked — nothing
        # was SOLD, so it is not "sold out".
        status = 'unavailable'
    else:
        status = demand_band(free, total)
    row.update({'status': status, 'reason': None, 'freeSlots': free, 'totalSlots': total})
    return row


def summary(listing, start, days, *, viewer=None):
    from venues.business import base_listing
    from venues.models import VenueBusinessSettings

    base = base_listing(listing)
    settings = VenueBusinessSettings.objects.filter(listing=base).first()
    today = today_ist()
    now_minute = now_minutes_ist()
    rows = [
        day_row(listing, start + datetime.timedelta(days=i), settings=settings,
                base=base, viewer=viewer, today=today, now_minute=now_minute)
        for i in range(days)
    ]
    return {
        'from': start.isoformat(),
        'days': rows,
        'slotMinutes': GRID_MINUTES,
    }
