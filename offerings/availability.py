"""
When a resort package or a big screen surprise can be bought, and how much
of it is left — the ONE place these rules live. The public offerings
endpoint shows exactly what checkout will accept, and checkout re-runs the
same counts under the venue's row lock so two customers can never both take
the last place (the lock bookings.views / payments use for slot bookings).
"""
import datetime

from django.utils import timezone

from bookings.models import Booking
from bookings.slots import IST, now_minutes_ist, today_ist

from .schema import minutes

WEEKDAY_KEYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')


def clock(minute):
    return f'{minute // 60:02d}:{minute % 60:02d}'


def active_bookings(listing, date, kind):
    """Rows of this kind that HOLD a place: everything except dead rows and
    expired payment holds — the same rule bookings.views._active_bookings
    uses for slots, so a held checkout blocks for 5 minutes and no longer
    (bookings/holds.py)."""
    from bookings.holds import expired_hold_q
    from bookings.views import exclude_dead
    return exclude_dead(
        Booking.objects.filter(listing=listing, date=date, kind=kind)
    ).exclude(expired_hold_q())


def _in_closures(closures, date):
    text = date.isoformat()
    for entry in closures or []:
        if entry.get('from', '') <= text <= entry.get('to', entry.get('from', '')):
            return entry.get('reason') or 'Closed'
    return None


def date_problem(settings, date, *, window=(None, None), days_key='availableDays'):
    """Why `date` cannot be booked, or None. `window` is the row's own
    (from, to) dates; `settings` the venue profile's days and closures."""
    if date < today_ist():
        return 'That date has passed.'
    start, end = window
    if start and date < start:
        return f'Available from {start:%d %b %Y}.'
    if end and date > end:
        return f'Available until {end:%d %b %Y}.'
    days = (settings or {}).get(days_key) or []
    if days and WEEKDAY_KEYS[date.weekday()] not in days:
        return 'Not available on this day of the week.'
    closed = _in_closures((settings or {}).get('closures'), date)
    if closed:
        return f'Unavailable on this date ({closed}).'
    return None


# --- resort packages ----------------------------------------------------------

def package_slots(package):
    """[(start_minute, end_minute)] a package can be booked for."""
    if package.kind == 'day' and package.duration == 'full_day':
        if package.check_in and package.check_out:
            return [(minutes(package.check_in), minutes(package.check_out))]
        return []
    return [(minutes(s['start']), minutes(s['end'])) for s in package.slots or []]


def package_guests(package, date, start, end, exclude=None, booked_only=False):
    """Guests already holding this package + date + slot. booked_only
    leaves out other customers' live 5-minute checkout holds — to tell
    "temporarily reserved" from "fully booked"."""
    from .models import PackageBooking
    live = active_bookings(package.listing, date, 'package')
    if booked_only:
        live = live.exclude(status='payment_pending')
    rows = PackageBooking.objects.filter(
        package=package, start_minute=start, end_minute=end,
        booking__in=live,
    )
    if exclude is not None:
        rows = rows.exclude(booking=exclude)
    return sum(row.adults + row.children for row in rows)


def package_slot_rows(package, settings, date):
    """Availability of every slot of one package on one date."""
    problem = date_problem(settings, date, window=(package.available_from, package.available_to))
    out = []
    today = today_ist()
    for start, end in package_slots(package):
        reason = problem
        if not reason and date == today and start <= now_minutes_ist():
            reason = 'This slot has already started.'
        remaining = None
        if package.capacity is not None:
            remaining = max(0, package.capacity - package_guests(package, date, start, end))
            if not reason and remaining < package.min_persons:
                booked_left = package.capacity - package_guests(package, date, start, end, booked_only=True)
                reason = (
                    'Temporarily reserved — other guests are checking out. Try again in a few minutes.'
                    if booked_left >= package.min_persons else 'Fully booked.'
                )
        out.append({
            'start': clock(start), 'end': clock(end),
            'available': reason is None, 'reason': reason, 'remaining': remaining,
        })
    return out


# --- big screen surprise ------------------------------------------------------

def show_date_problem(settings, show, date):
    """The theatre's dates / days / closures AND the show's own run."""
    problem = date_problem(settings, date, window=(_iso(settings.get('availableFrom')), _iso(settings.get('availableTo'))))
    if problem:
        return problem
    if show.available_from and date < show.available_from:
        return f'This show runs from {show.available_from:%d %b %Y}.'
    if show.available_to and date > show.available_to:
        return f'This show ran until {show.available_to:%d %b %Y}.'
    return None


def _iso(text):
    return datetime.date.fromisoformat(text) if text else None


def screen_bookings(listing, date, screen, minute, exclude=None):
    from .models import SurpriseBooking
    rows = SurpriseBooking.objects.filter(
        screen=screen, show_minute=minute,
        booking__in=active_bookings(listing, date, 'surprise'),
    )
    if exclude is not None:
        rows = rows.exclude(booking=exclude)
    return rows.count()


def day_bookings(listing, date, exclude=None):
    rows = active_bookings(listing, date, 'surprise')
    if exclude is not None:
        rows = rows.exclude(pk=exclude.pk)
    return rows.count()


def lead_problem(settings, date, minute):
    from .catalog import DEFAULT_LEAD_HOURS
    lead = settings.get('minLeadHours')
    lead = DEFAULT_LEAD_HOURS if lead is None else lead
    show_at = datetime.datetime.combine(date, datetime.time(minute // 60, minute % 60), tzinfo=IST)
    if show_at - timezone.now() < datetime.timedelta(hours=lead):
        if lead:
            return f'Surprises must be booked at least {lead} hour{"s" if lead != 1 else ""} before the show.'
        return 'This show has already started.'
    return None


def show_time_rows(listing, settings, show, date):
    problem = show_date_problem(settings, show, date)
    per_show = settings.get('maxBookingsPerShow') or 1
    per_day = settings.get('showsPerDay')
    day_full = per_day is not None and day_bookings(listing, date) >= per_day
    out = []
    for text in show.times or []:
        minute = minutes(text)
        reason = problem or lead_problem(settings, date, minute)
        remaining = max(0, per_show - screen_bookings(listing, date, show.screen, minute))
        if not reason and day_full:
            reason = 'All of today’s surprise slots are booked.'
        if not reason and remaining == 0:
            reason = 'Booked.'
        out.append({'time': text, 'available': reason is None, 'reason': reason, 'remaining': remaining})
    return out


def booking_conflicts(booking):
    """For bookings.payments._conflicts_excluding_self: True when the place
    this (expired-hold) booking reserved has been taken meanwhile, so a late
    payment is refunded instead of overselling."""
    kind = booking.kind
    if kind == 'package':
        detail = getattr(booking, 'package_booking', None)
        if detail is None or detail.package.capacity is None:
            return False
        taken = package_guests(detail.package, booking.date, detail.start_minute, detail.end_minute, exclude=booking)
        return taken + detail.persons > detail.package.capacity
    if kind == 'surprise':
        detail = getattr(booking, 'surprise_booking', None)
        if detail is None:
            return False
        profile = getattr(booking.listing, 'offering_profile', None)
        settings = profile.settings if profile else {}
        per_show = settings.get('maxBookingsPerShow') or 1
        if screen_bookings(booking.listing, booking.date, detail.screen, detail.show_minute, exclude=booking) >= per_show:
            return True
        per_day = settings.get('showsPerDay')
        return per_day is not None and day_bookings(booking.listing, booking.date, exclude=booking) >= per_day
    return False                          # memberships never sell out
