"""
When a booking's payment ACTUALLY succeeded — kept apart from created_at.

created_at is when the customer started the booking (the slot hold). paid_at
is when the successful Razorpay payment for it was recorded, which can be
minutes later (UPI approval) or never (abandoned / failed). The two are never
substituted for one another.

paid_at_source says where the time came from, so an admin can tell how exact
it is:

  'gateway'   Razorpay's own timestamp on the signed payment.captured webhook
              (the payload's `created_at`). The provider's time for the
              capture — the most exact value available.
  'checkout'  The moment this server verified the checkout callback's
              payment signature. Set when the customer's browser reports the
              payment before the webhook arrives.
  'webhook'   The moment this server received a signed payment.captured
              webhook that carried no usable timestamp of its own.

Rules (stamp_payment_time):
  * Only the payment id now recorded on the booking is stamped, so after a
    failed attempt and a successful retry the time belongs to the payment
    that succeeded.
  * A server-side time is written once; a later gateway time for the SAME
    payment refines it. A gateway time is never replaced.
  * Nothing is stamped without a payment id. Walk-ins, pay-at-venue and
    unpaid bookings keep paid_at empty, and so do bookings paid before this
    field existed — the time was never recorded, so it is never guessed.
"""
import datetime

from django.utils import timezone

GATEWAY = 'gateway'
CHECKOUT = 'checkout'
WEBHOOK = 'webhook'
SOURCES = (GATEWAY, CHECKOUT, WEBHOOK)

# A webhook timestamp outside this window is treated as unusable.
_EARLIEST = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
_FUTURE_SLACK = datetime.timedelta(minutes=10)


def gateway_time(payload):
    """The signed webhook payload's own `created_at` (unix seconds) as an
    aware UTC datetime, or None when it is absent or implausible."""
    raw = payload.get('created_at') if isinstance(payload, dict) else None
    if isinstance(raw, bool):
        return None
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    try:
        moment = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    if moment < _EARLIEST or moment > timezone.now() + _FUTURE_SLACK:
        return None
    return moment


def webhook_time(gateway_moment):
    """(when, source) for a capture learned from the webhook."""
    if gateway_moment is not None:
        return gateway_moment, GATEWAY
    return timezone.now(), WEBHOOK


def stamp_payment_time(row, payment_id, *, when=None, source):
    """Record when `row`'s successful payment `payment_id` happened.

    `row` is a Booking or an EventOrder. Sets row.paid_at / paid_at_source in
    memory and returns the field names to add to the caller's save
    (update_fields) — [] when nothing should change.
    """
    if not payment_id or row.razorpay_payment_id != payment_id:
        return []
    if row.paid_at is not None:
        if row.paid_at_source == GATEWAY or source != GATEWAY:
            return []
    row.paid_at = when or timezone.now()
    row.paid_at_source = source
    return ['paid_at', 'paid_at_source']


def payment_time_fields(row):
    """The camelCase keys every authorised booking record carries."""
    return {
        'paidAt': row.paid_at.isoformat() if row.paid_at else None,
        'paidAtSource': row.paid_at_source or None,
    }
