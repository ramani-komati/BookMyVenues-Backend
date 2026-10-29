"""
Who the customer calls about a booking — released ONLY once the payment has
been verified, and only on the customer's own booking-history responses.

Source of truth is the existing account data, never a copy:

  * venue bookings (every category: halls, theatres, play zones, resorts,
    PlayStation …) → the listing's owner: Booking.listing.vendor, i.e. the
    name and phone the vendor registered with;
  * event tickets → the vendor who submitted the event (Event.vendor). An
    event the admin created has no vendor account: its organiser contact is
    the name + phone the admin recorded on it (organizer_name /
    organizer_phone, else the event's contact_phone). If none exists, nothing is shown — the hosting
    venue's owner is NOT assumed to be the organiser.

"Paid" is the server's own record, never anything the browser says:
status 'confirmed' (set only by the signature-verified payment callback or
the signed webhook) AND a captured Razorpay payment id. Pending, failed,
cancelled, removed, refund-pending and refunded bookings get nothing — a
refund revokes the contact along with the booking. Legacy pay-at-venue rows
and vendor walk-ins never went through the gateway, so they get nothing
either.
"""
from .models import Booking


def _person(name, phone, role):
    phone = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if not phone:
        return None
    return {'role': role, 'name': str(name or '').strip() or None, 'phone': phone}


def booking_is_paid(booking):
    return (
        booking.status == 'confirmed'
        and booking.method in Booking.CUSTOMER_METHODS
        and bool(booking.razorpay_payment_id)
    )


def booking_contact(booking):
    """{role, name, phone} of the venue's owner, or None until paid."""
    if not booking_is_paid(booking) or booking.listing_id is None:
        return None
    vendor = booking.listing.vendor
    return _person(vendor.name, vendor.phone, 'Venue owner')


def order_is_paid(order):
    return order.status == 'confirmed' and bool(order.razorpay_payment_id)


def order_contact(order):
    """{role, name, phone} of the event's organiser, or None until paid."""
    if not order_is_paid(order) or order.event_id is None:
        return None
    event = order.event
    if event.vendor_id:
        return _person(event.vendor.name, event.vendor.phone, 'Organiser')
    # Admin-created event: the organiser contact the admin recorded, else the
    # event's own contact number (also admin-entered).
    return _person(
        event.organizer_name or event.name,
        event.organizer_phone or event.contact_phone,
        'Organiser',
    )
