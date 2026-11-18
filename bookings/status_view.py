"""
Booking status vs payment status — two separate answers about one row.

Venue bookings and event orders store ONE lifecycle value (`status`:
payment_pending / confirmed / failed / cancelled / refund_pending / refunded /
removed) and derive 'completed' from the clock (display_status). Every screen
used to turn that single value into a single label, and the products
disagreed: a paid venue booking read "Confirmed" while a paid event ticket
read "Paid", and a refunded booking could still read "Confirmed".

This module answers the two questions separately, from the SERVER's record
only — nothing a client sends can move either answer:

  booking_state — is the reservation happening?
      pending        unpaid, inside its payment hold
      expired        unpaid hold that lapsed (abandoned checkout)
      not_confirmed  the payment attempt failed
      confirmed      paid / accepted, slot or show still ahead
      completed      confirmed and its last slot / the show has ended
      cancelled      cancelled, refund pending or refunded
      removed        an unpaid row a super admin removed

  payment_state — what happened to the money?
      pending        awaiting the gateway
      unpaid         nothing was captured (abandoned / cancelled unpaid)
      failed         the gateway reported a failed attempt
      completed      a gateway capture is recorded (razorpay_payment_id)
      refund_pending captured, owed back to the customer
      refunded       captured and returned
      at_venue       never charged online: pay-at-venue or a walk-in
      free           nothing to pay (₹0)
      not_recorded   a confirmed legacy row with no gateway capture

'completed' as a PAYMENT means "a verified capture exists" — never "the
booking happened"; 'completed' as a BOOKING means "the time has passed" —
never "the money arrived". A confirmed booking is only payment-'completed'
when a capture id is stored, because 'confirmed' is set solely by the
signature-verified checkout callback or the signed webhook (payments.py),
except for walk-ins and legacy pay-at-venue rows, which are named as such.

These are API fields only (no new database values). Labels live here too so
the exports and every client read the same words.
"""

BOOKING_STATE_LABEL = {
    'pending': 'Pending',
    'expired': 'Expired',
    'not_confirmed': 'Not confirmed',
    'confirmed': 'Confirmed',
    'completed': 'Completed',
    'cancelled': 'Cancelled',
    'removed': 'Removed',
}

PAYMENT_STATE_LABEL = {
    'pending': 'Pending',
    'unpaid': 'Not paid',
    'failed': 'Failed',
    'completed': 'Completed',
    'refund_pending': 'Refund pending',
    'refunded': 'Refunded',
    'at_venue': 'Pay at venue',
    'free': 'No payment needed',
    'not_recorded': 'No online payment',
}

_CANCELLED = ('cancelled', 'refund_pending', 'refunded')


def _states(status, display, *, hold_expired, captured, offline, amount):
    """(booking_state, payment_state) for one row's stored facts."""
    status = str(status or '')
    if status == 'removed':
        return 'removed', 'unpaid'
    if status == 'payment_pending' or display == 'expired':
        return ('expired', 'unpaid') if hold_expired or display == 'expired' else ('pending', 'pending')
    if status == 'failed':
        return 'not_confirmed', 'failed'
    if status in _CANCELLED:
        if status == 'cancelled':
            # A customer may cancel a PAID booking: the capture stays on the
            # row until the refund is issued, and the money is still held.
            return 'cancelled', 'completed' if captured else 'unpaid'
        return 'cancelled', status
    if status == 'confirmed':
        booking = 'completed' if display == 'completed' else 'confirmed'
        if offline:
            return booking, 'at_venue'
        if captured:
            return booking, 'completed'
        if not amount:
            return booking, 'free'
        return booking, 'not_recorded'
    # Unknown stored value: echo it rather than coerce it into a paid state.
    return status or 'unknown', status or 'unknown'


def booking_states(booking):
    """(booking_state, payment_state) for a venue Booking."""
    offline = bool(booking.walk_in) or booking.method in ('venue', 'walk-in')
    return _states(
        booking.status, booking.display_status,
        hold_expired=booking.hold_expired,
        captured=bool(booking.razorpay_payment_id),
        offline=offline and not booking.razorpay_payment_id,
        amount=booking.amount,
    )


def order_states(order):
    """(booking_state, payment_state) for an events EventOrder. Tickets are
    only ever paid online, so there is no at-venue case."""
    return _states(
        order.status, order.display_status,
        hold_expired=order.hold_expired,
        captured=bool(order.razorpay_payment_id),
        offline=False,
        amount=order.amount,
    )


def state_fields(states):
    """The additive camelCase keys every record carries."""
    booking, payment = states
    return {
        'bookingState': booking,
        'bookingStateLabel': BOOKING_STATE_LABEL.get(booking, booking),
        'paymentState': payment,
        'paymentStateLabel': PAYMENT_STATE_LABEL.get(payment, payment),
    }
