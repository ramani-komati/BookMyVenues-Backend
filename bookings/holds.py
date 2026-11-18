"""
The 5-minute slot hold — ONE place for its rules.

A hold is not a separate table: it is the Booking row itself in status
'payment_pending' (the row payment verification later confirms), so a hold
and a booking can never disagree about who owns a slot. What this module adds:

  * HOLD_MINUTES = 5, measured on the SERVER clock from when the row was
    created and stored in Booking.hold_expires_at. Nothing extends it — not a
    refresh, not a second device, not starting the payment (a repeat request
    for the same slot returns the SAME hold with the SAME expiry).
  * expiry needs no job: every availability / overlap query treats an expired
    hold as free the moment it lapses (live_hold_q). release_expired_holds
    only tidies rows that can never be paid any more.
  * two customers can never both hold one slot: every hold is created inside
    transaction.atomic() under a row lock on the venue's Listing
    (select_for_update), and the overlap check is re-read under that lock —
    the same rule every venue booking path already uses.

Hold kinds, as the admin panel distinguishes them (hold_state):
  held             reserved at checkout, payment not started yet
  payment_pending  the Razorpay order exists, payment in progress
  expired          the 5 minutes passed without a verified payment
A late payment after expiry is confirmed only if the slot is still free;
otherwise it is queued for refund (bookings/payments.py) — never a double
booking.

Event tickets keep their own seat-hold window (events/inventory.py); gym
memberships never sell out, so neither uses this exclusive hold.
"""
import datetime

from django.db.models import Q
from django.utils import timezone

HOLD_MINUTES = 5
HOLD = datetime.timedelta(minutes=HOLD_MINUTES)
# One customer cannot sit on more than this many slots at once (across
# venues) — stops one account reserving a whole evening everywhere.
MAX_LIVE_HOLDS_PER_USER = 3


def now():
    return timezone.now()


def new_expiry(at=None):
    return (at or now()) + HOLD


def expires_at(booking):
    """When this row's hold lapses. Rows created before hold_expires_at
    existed fall back to created_at + HOLD_MINUTES."""
    if booking.hold_expires_at is not None:
        return booking.hold_expires_at
    if booking.created_at is None:
        return None
    return booking.created_at + HOLD


def live_hold_q(at=None):
    """Q for payment_pending rows whose hold is still running."""
    at = at or now()
    return Q(status='payment_pending') & (
        Q(hold_expires_at__gt=at)
        | Q(hold_expires_at__isnull=True, created_at__gt=at - HOLD)
    )


def expired_hold_q(at=None):
    """Q for payment_pending rows whose hold has lapsed — to EXCLUDE from
    everything that decides whether a slot is taken."""
    at = at or now()
    return Q(status='payment_pending') & (
        Q(hold_expires_at__lte=at)
        | Q(hold_expires_at__isnull=True, created_at__lte=at - HOLD)
    )


def is_live(booking, at=None):
    if booking.status != 'payment_pending':
        return False
    ends = expires_at(booking)
    return ends is not None and ends > (at or now())


def hold_state(booking, at=None):
    """'held' | 'payment_pending' | 'expired' for an unpaid row, else None."""
    if booking.status != 'payment_pending':
        return None
    if not is_live(booking, at):
        return 'expired'
    return 'payment_pending' if booking.razorpay_order_id else 'held'


def seconds_left(booking, at=None):
    ends = expires_at(booking)
    if ends is None or booking.status != 'payment_pending':
        return 0
    return max(0, int((ends - (at or now())).total_seconds()))


def hold_payload(booking, at=None):
    """What the customer's checkout shows — the countdown runs from these,
    but the server's own clock stays the authority."""
    at = at or now()
    ends = expires_at(booking)
    state = hold_state(booking, at)
    if state is None:
        state = {
            'confirmed': 'confirmed', 'cancelled': 'released', 'failed': 'failed',
        }.get(booking.status, booking.status)
    return {
        'id': booking.id,
        'state': state,
        'venueId': str(booking.listing_id) if booking.listing_id else None,
        'date': booking.date.isoformat(),
        'slots': booking.slots,
        'sport': booking.sport or None,
        'unit': booking.unit,
        'expiresAt': ends.isoformat() if ends else None,
        'serverNow': at.isoformat(),
        'secondsLeft': seconds_left(booking, at),
        'holdMinutes': HOLD_MINUTES,
    }


def release_expired_holds(at=None):
    """Tidy-up (manage.py release_expired_holds; safe to run any time).

    Availability never waits for this — expired holds are already free. It
    only cancels expired holds that NEVER reached the gateway (no Razorpay
    order, so no payment can ever arrive for them). Expired holds WITH an
    order stay payment_pending, because a late UPI capture must still find
    its row: it is confirmed if the slot is still free, refunded otherwise.
    Returns how many rows were released."""
    from .models import Booking
    return Booking.objects.filter(expired_hold_q(at), razorpay_order_id='').update(status='cancelled')
