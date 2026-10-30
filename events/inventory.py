"""
Ticket inventory — the ONE place that decides whether seats are still free.

THE CRITICAL RULE (the mirror image of the venue booking rule): many
customers may buy into the same event date and time; exactly as many as the
capacity allows, never one more. Two requests racing for the last two seats
must BOTH succeed; three racing for the last two must leave one with a 409.

How that is guaranteed:
  * every seat decision happens inside transaction.atomic()
  * the EventDate row is locked with select_for_update(), so concurrent
    requests for the SAME date queue up behind each other (the identical
    technique bookings/payments.py uses on Listing)
  * the sold count is RE-READ inside the lock, never carried in from the
    read-only availability call the customer's browser made

UNLIMITED dates (capacity NULL) skip all of that: any number of customers
may register at the same moment, so there is nothing to lock or count.

Unpaid Razorpay holds count against capacity for PENDING_HOLD_MINUTES and
then stop, so an abandoned checkout releases its seats automatically. The
hold window is imported from bookings so events and venues can never drift
to different expiry rules.
"""
import datetime

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from bookings.slots import today_ist
from bookings.views import PENDING_HOLD_MINUTES

from .models import Event, EventDate, EventOrder

# A single order can never take more than this, whatever the ticket types
# allow. Stops one request from vacuuming up a whole event's inventory.
MAX_TICKETS_PER_ORDER = 20


class InventoryError(Exception):
    """Seats cannot be sold. `code` is echoed to the client next to the
    message so the frontend branches on it instead of matching text."""

    def __init__(self, message, code='', remaining=None):
        super().__init__(message)
        self.code = code
        self.remaining = remaining


def live_orders(event_date):
    """Orders that HOLD seats on this date: everything except dead rows
    (cancelled / refunded) and EXPIRED payment holds.

    Availability and the oversell check both read exactly this queryset, so
    the number a customer is shown and the number they are checked against
    can never disagree — the same guarantee bookings._active_bookings gives.
    """
    cutoff = timezone.now() - datetime.timedelta(minutes=PENDING_HOLD_MINUTES)
    return (
        EventOrder.objects.filter(event_date=event_date)
        .exclude(status__in=EventOrder.DEAD_STATUSES)
        .exclude(status='payment_pending', created_at__lt=cutoff)
    )


def seats_sold(event_date, exclude_order_id=None):
    """Capacity units already taken on this date.

    `exclude_order_id` leaves one order out — used when re-checking an order
    that is ALREADY holding its own seats (payment verification), which would
    otherwise always find itself and double-count.
    """
    queryset = live_orders(event_date)
    if exclude_order_id:
        queryset = queryset.exclude(pk=exclude_order_id)
    return queryset.aggregate(total=Sum('seats'))['total'] or 0


def seats_remaining(event_date, exclude_order_id=None):
    """Free capacity units, never negative — None for an unlimited date."""
    if event_date.capacity is None:
        return None
    return max(0, event_date.capacity - seats_sold(event_date, exclude_order_id))


def remaining_after(event_date, sold):
    """capacity - sold for display, None when the date is unlimited."""
    if event_date.capacity is None:
        return None
    return max(0, event_date.capacity - sold)


def date_is_sellable(event, event_date, today=None):
    """(ok, message) — may a customer buy this date RIGHT NOW?

    Checks everything that does not depend on how many seats they want, so
    both the availability read and the order write apply identical rules:
      * the event itself is live
      * the vendor has not closed this particular night
      * the date is today or later (never the past)
      * today falls inside the event's selling window
    """
    today = today or today_ist()

    if event.status not in Event.BOOKABLE_STATUSES:
        return False, 'This event is not open for booking.'
    if not event_date.is_available:
        return False, 'This date is not available for booking.'
    if event_date.date < today:
        return False, 'Cannot book a past date.'
    if event.booking_opens_on and today < event.booking_opens_on:
        return False, (
            f'Bookings for this event open on '
            f'{event.booking_opens_on:%d %b %Y}.'
        )
    if event.booking_closes_on and today > event.booking_closes_on:
        return False, 'Bookings for this event have closed.'
    return True, ''


@transaction.atomic
def reserve_seats(event, event_date_id, seats, *, exclude_order_id=None):
    """Lock the date and confirm `seats` capacity units are available.

    Returns the LOCKED EventDate. Raises InventoryError when the date cannot
    be sold or the seats are gone. The caller MUST create (or confirm) its
    order inside the same transaction — the lock is only held until commit.

    This is the whole oversell defence, so it deliberately re-reads the sold
    count from the database rather than trusting anything the caller passed.
    """
    try:
        plain = EventDate.objects.select_related('event').get(pk=event_date_id, event=event)
    except EventDate.DoesNotExist:
        raise InventoryError('That event date was not found.', code='DATE_NOT_FOUND')

    if plain.capacity is None:
        # Unlimited: registrations never wait on each other — no row lock,
        # nothing to count. (The per-order ticket limit is checked when the
        # order is priced.)
        ok, message = date_is_sellable(event, plain)
        if not ok:
            raise InventoryError(message, code='DATE_UNAVAILABLE')
        return plain

    locked = (
        EventDate.objects.select_for_update()
        .select_related('event')
        .get(pk=event_date_id, event=event)
    )
    ok, message = date_is_sellable(event, locked)
    if not ok:
        raise InventoryError(message, code='DATE_UNAVAILABLE')

    remaining = seats_remaining(locked, exclude_order_id)
    if remaining is None:          # made unlimited a moment ago
        return locked
    if seats > remaining:
        raise InventoryError(
            'Only {} ticket{} left for this date.'.format(
                remaining, '' if remaining == 1 else 's'
            ) if remaining else 'This event date is sold out.',
            code='SOLD_OUT',
            remaining=remaining,
        )
    return locked


def availability(event, event_date, *, include_counts=False):
    """The {date, soldOut, available, reason} payload for one night.

    Deliberately UNCACHED by its caller — a stale answer about whether a
    night can still be bought would be a lie, exactly as for venue slots.

    `include_counts` adds capacity / sold / remaining and is OFF for the
    public endpoint: the product decision is that customers see no
    remaining-seat counter, so the number does not leave the server. The
    enforcement is unchanged — reserve_seats below still re-reads the sold
    count under a row lock, which is what actually prevents overselling.
    """
    sold = seats_sold(event_date)
    remaining = remaining_after(event_date, sold)
    sold_out = remaining == 0            # never for an unlimited date (None)
    ok, message = date_is_sellable(event, event_date)
    payload = {
        'date': event_date.date.isoformat(),
        'soldOut': sold_out,
        'available': ok and not sold_out,
        'reason': '' if ok else message,
    }
    if include_counts:
        payload.update({
            'capacity': event_date.capacity,
            'unlimited': event_date.capacity is None,
            'sold': sold,
            'remaining': remaining,
        })
    return payload
