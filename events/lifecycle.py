"""
Ended events leave the catalogue by themselves (QA audit 2026-10-30: events
whose last night had passed were still marked live).

An event ENDS when its LAST date's show is over (IST): the last date is in
the past, or it is today and the end time has passed. A show whose end time
is not after its start time runs past midnight, so it ends the next morning.

archive_ended_events() moves such LIVE events to `ended`: hidden from the
public list, never bookable (BOOKABLE_STATUSES), featured promotion dropped
(Event.save). Nothing is deleted — orders, tickets, invoices and check-in
keep working, and the detail page still opens (marked ended) for people who
hold the link. An admin can publish it again after adding new dates.

It runs at most once per SWEEP_SECONDS from the public event list / detail
and the admin event list, and on demand via
`python manage.py archive_ended_events` (cron-friendly).
"""
import datetime
import logging

from django.core.cache import cache
from django.db.models import Max

from bookings.slots import IST

from .models import Event

logger = logging.getLogger(__name__)

SWEEP_KEY = 'events:ended-sweep'
SWEEP_SECONDS = 5 * 60


def event_end(last_date, start_time, end_time):
    """The aware IST moment the event's last show is over."""
    if last_date is None:
        return None
    end_day = last_date
    if start_time and end_time and end_time <= start_time:
        end_day = last_date + datetime.timedelta(days=1)   # runs past midnight
    return datetime.datetime.combine(end_day, end_time or datetime.time.max, tzinfo=IST)


def ended_ids(now=None):
    now = now or datetime.datetime.now(tz=IST)
    rows = (Event.objects.filter(status=Event.Status.LIVE)
            .annotate(last_date=Max('dates__date'))
            .values_list('pk', 'last_date', 'start_time', 'end_time'))
    return [pk for pk, last, start, end in rows
            if last is not None and event_end(last, start, end) <= now]


def archive_ended_events(now=None):
    """LIVE events that are over → ENDED. Returns how many moved."""
    moved = 0
    for event in Event.objects.filter(pk__in=ended_ids(now)):
        event.status = Event.Status.ENDED
        event.save(update_fields=['status', 'updated_at'])
        moved += 1
    if moved:
        logger.info('Archived %s ended event(s).', moved)
    return moved


def maybe_archive_ended_events():
    """The throttled sweep the list views call — never raises."""
    if not cache.add(SWEEP_KEY, True, SWEEP_SECONDS):
        return 0
    try:
        return archive_ended_events()
    except Exception:  # noqa: BLE001 — a sweep must never break a page
        logger.exception('Ended-event sweep failed')
        return 0
