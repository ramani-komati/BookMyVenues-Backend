"""
Booking verification and check-in at the gate.

ONE set of rules, shared by the vendor portal and the super-admin panel, over
the ONE booking ID the platform issues for every booking type — the existing
6-character code ("K7M2QX", bookings/booking_codes.py), unique across venue
bookings and event orders alike:

  * venue bookings (every category, walk-ins included) — Booking.id;
  * event tickets — EventOrder.id. One ID covers the whole order: tickets
    carry no IDs of their own, so an order is admitted as one party
    ("5 tickets — admits 5"), never ticket by ticket.

Bookings made before the shared format keep their original IDs ('bk_…' venue
bookings, 'eo_…' event orders) and are still verified by them — no ID is ever
re-issued or rewritten.

A booking may enter only when ALL of these hold:

  1. the ID exists;
  2. it is for the venue / event being checked (the gate the vendor picked);
  3. the vendor manages that venue / event (Listing.vendor, or the event
     visibility rule events.vendor_views.visible_events already enforces);
  4. payment and confirmation allow entry — stored status 'confirmed', which
     only a verified Razorpay payment (or the vendor's own walk-in entry)
     ever sets;
  5. it is not cancelled, refunded, refund-pending or admin-removed;
  6. it has not been checked in already;
  7. it is for TODAY (IST) — a ticket for Saturday does not open the gate
     on Friday, and a past booking cannot be re-used.

Wrong-venue answers never describe another vendor's booking: "Invalid for
this venue" and nothing more. The super admin's verify() sees everything but
NEVER writes; only check_in() records entry, under a row lock plus a
conditional UPDATE, so concurrent attempts admit a booking at most once.
"""
import datetime
import logging
import re

from django.db import transaction
from django.utils import timezone

from .models import Booking
from .slots import IST, today_ist

logger = logging.getLogger(__name__)

# --- Verdict codes (the API contract; the UIs switch on these) --------------
VALID = 'VALID'
CHECKED_IN = 'CHECKED_IN'
INVALID_ID = 'INVALID_ID'
SHORT_REF = 'SHORT_REF'
WRONG_VENUE = 'WRONG_VENUE'
PAYMENT_INCOMPLETE = 'PAYMENT_INCOMPLETE'
CANCELLED = 'CANCELLED'
REFUNDED = 'REFUNDED'
ALREADY_CHECKED_IN = 'ALREADY_CHECKED_IN'
WRONG_DATE = 'WRONG_DATE'
INELIGIBLE = 'INELIGIBLE'

# Stored status -> the verdict when it blocks entry. 'confirmed' is the only
# status that admits; anything not listed here is INELIGIBLE.
_STATUS_VERDICT = {
    'payment_pending': PAYMENT_INCOMPLETE,
    'failed': PAYMENT_INCOMPLETE,
    'cancelled': CANCELLED,
    'removed': CANCELLED,
    'refund_pending': REFUNDED,
    'refunded': REFUNDED,
}

_TITLES = {
    VALID: 'Valid — ready to check in',
    CHECKED_IN: 'Checked in',
    INVALID_ID: 'Invalid booking ID',
    SHORT_REF: 'Full booking ID needed',
    WRONG_VENUE: 'Invalid for this venue',
    PAYMENT_INCOMPLETE: 'Payment not completed',
    CANCELLED: 'Booking cancelled',
    REFUNDED: 'Booking refunded',
    ALREADY_CHECKED_IN: 'Already checked in',
    WRONG_DATE: 'Not valid today',
    INELIGIBLE: 'Not valid for entry',
}

_HEX = frozenset('0123456789ABCDEF')
_LONG_ID = re.compile(r'(bk|wk|eo)[-_]?([0-9a-f]{12})')
_BARE_HEX = re.compile(r'[0-9a-f]{12}')
_SHORT_ID = re.compile(r'[A-Z0-9]{6}')
_INVOICE_REF = re.compile(r'(BMV-?)?[0-9A-F]{6}')
MAX_INPUT = 40


class GateError(Exception):
    """The venue / event the vendor says they are checking does not exist or
    is not theirs. A request error (404), not a verdict about the booking."""


def candidate_ids(raw):
    """[(kind, pk), …] the typed text could be — or ([], reason).

    Forgiving about how people copy an ID: spaces, a leading '#', any case.
    A 6-character ID can be a venue booking OR an event order (one shared
    format, unique across both — BookingCode), so both tables are asked.
    Older long IDs ('bk_…', 'eo_…', 'BK-…', a bare 12-hex tail) still work.

    A 6-character ALL-HEX code is a legacy venue booking's short invoice
    reference ('BMV-3F8A92'): not unique, so it is refused with SHORT_REF
    instead of being matched. New IDs always contain a non-hex letter, so
    they can never look like one.
    """
    text = ''.join(str(raw or '').split()).lstrip('#')
    if not text or len(text) > MAX_INPUT:
        return [], INVALID_ID
    lower, upper = text.lower(), text.upper()
    match = _LONG_ID.fullmatch(lower)
    if match:
        prefix, tail = match.groups()
        kind = 'event' if prefix == 'eo' else 'venue'
        return [(kind, f'{prefix}_{tail}')], None
    if _BARE_HEX.fullmatch(lower):
        return [('venue', f'bk_{lower}'), ('venue', f'wk_{lower}'), ('event', f'eo_{lower}')], None
    if _SHORT_ID.fullmatch(upper) and not _HEX.issuperset(upper):
        return [('venue', upper), ('event', upper)], None
    if _INVOICE_REF.fullmatch(upper):
        return [], SHORT_REF
    return [], INVALID_ID


def find_booking(raw):
    """(kind, row) for a typed booking ID, or (None, reason)."""
    from events.models import EventOrder

    candidates, reason = candidate_ids(raw)
    found = []
    for kind, pk in candidates:
        model = Booking if kind == 'venue' else EventOrder
        row = model.objects.filter(pk=pk).first()
        if row is not None:
            found.append((kind, row))
    if len(found) == 1:
        return found[0]
    if found:
        # BookingCode makes this impossible for IDs issued by this code; a
        # hand-made duplicate must never be guessed at — refuse it loudly.
        logger.error('checkin: booking ID %r matches %d bookings', str(raw)[:MAX_INPUT], len(found))
    return None, reason or INVALID_ID


# --- Gates ------------------------------------------------------------------

def vendor_gates(user):
    """What a vendor can check customers in AT: their own venues (base
    listings — "— Pitch 2" units fold into their venue) and the events they
    may see. Drives the portal's "Checking in at" picker."""
    from events.vendor_views import visible_events
    from venues.models import Listing

    venues = []
    for listing in user.listings.exclude(status=Listing.Status.DELETED).order_by('name'):
        detail = (listing.record or {}).get('detail') or {}
        if isinstance(detail, dict) and str(detail.get('unitOf') or '').strip():
            continue
        venues.append({
            'kind': 'venue', 'id': str(listing.id),
            'name': listing.record.get('name') or listing.name,
            'status': listing.status,
        })
    events = [
        {'kind': 'event', 'id': str(event.id), 'name': event.name, 'status': event.status}
        for event in visible_events(user).order_by('name')
    ]
    return venues + events


def resolve_gate(user, venue_id=None, event_id=None):
    """('venue', Listing) or ('event', Event) the vendor is checking at.

    Raises GateError for a missing, malformed or foreign id — a foreign venue
    reads as "not found", never as "not yours"."""
    import uuid

    from events.vendor_views import visible_events
    from venues.models import Listing

    if bool(venue_id) == bool(event_id):
        raise GateError('Choose the venue or the event you are checking in at.')
    try:
        pk = uuid.UUID(str(venue_id or event_id))
    except (ValueError, TypeError):
        raise GateError('Venue not found.') from None
    if venue_id:
        listing = (
            user.listings.exclude(status=Listing.Status.DELETED).filter(pk=pk).first()
        )
        if listing is None:
            raise GateError('Venue not found.')
        return 'venue', listing
    event = visible_events(user).filter(pk=pk).first()
    if event is None:
        raise GateError('Event not found.')
    return 'event', event


def _venue_family(listing):
    """Listing ids of the physical venue: its base plus "— Pitch N" units."""
    from .ratings import venue_set_ids
    return venue_set_ids(listing)


def _gate_name(gate):
    kind, row = gate
    if kind == 'venue':
        return (row.record or {}).get('name') or row.name
    return row.name


def _vendor_manages(user, kind, row):
    """Requirement 3: may this vendor run the door for this booking at all?"""
    if kind == 'venue':
        return row.listing_id is not None and row.listing.vendor_id == user.id
    from events.vendor_views import visible_events
    return row.event_id is not None and visible_events(user).filter(pk=row.event_id).exists()


def _matches_gate(kind, row, gate):
    """Requirement 2: is this booking for the venue / event being checked?"""
    gate_kind, gate_row = gate
    if gate_kind == 'event':
        return kind == 'event' and str(row.event_id) == str(gate_row.pk)
    family = _venue_family(gate_row)
    if kind == 'venue':
        return str(row.listing_id) in family
    # An event ticket presented at the venue the event is held at.
    return bool(row.event_id and row.event and row.event.listing_id
                and str(row.event.listing_id) in family)


def _booked_for(kind, row):
    """Name of the vendor's own venue / event a booking is actually for."""
    if kind == 'venue':
        return row.venue_name or (row.listing.name if row.listing_id else '')
    return row.event_name


# --- Payloads ---------------------------------------------------------------

def cash_due(row):
    """Cash a venue booking still owes at the door — the frontend's
    awaitsCollection rule: a legacy pay-at-venue booking owes everything, a
    part-paid one its at-venue slice, and nothing once the vendor ticked
    "cash collected". Entry is still allowed; the vendor collects it."""
    if row.collected:
        return 0
    if row.method == Booking.Method.VENUE:
        return row.amount
    return row.venue_due


def _ist(moment):
    return moment.astimezone(IST) if moment else None


def _date_label(day):
    return day.strftime('%a %d %b %Y')


def _masked_phone(phone):
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    return f'••••••{digits[-4:]}' if len(digits) >= 4 else None


def _person(user):
    if user is None:
        return None
    return user.name or user.phone or f'User {user.pk}'


def gate_details(kind, row):
    """What the person at the door needs to identify the party — for the
    vendor's OWN bookings only. Phone is masked to its last 4 digits."""
    base = {
        'kind': kind,
        'id': row.id,
        'date': row.date.isoformat(),
        'customerName': row.customer_name or None,
        'phoneLast4': _masked_phone(row.phone),
        'status': row.status,
        'amount': row.amount,
        'checkedInAt': row.checked_in_at.isoformat() if row.checked_in_at else None,
        'checkedInBy': _person(row.checked_in_by) if row.checked_in_by_id else None,
    }
    if kind == 'venue':
        base.update({
            'venueName': row.venue_name,
            'unitLabel': row.unit_label or None,
            'sport': row.sport or None,
            'slots': list(row.slots or []),
            'occasion': row.occasion or None,
            'walkIn': bool(row.walk_in or row.method == Booking.Method.WALK_IN),
            'dueAtVenue': cash_due(row),
        })
    else:
        base.update({
            'eventName': row.event_name,
            'startTime': row.start_time.strftime('%H:%M') if row.start_time else None,
            'endTime': row.end_time.strftime('%H:%M') if row.end_time else None,
            'tickets': row.ticket_count,
            'admits': row.seats,
            'items': [
                {'label': item.get('label'), 'qty': item.get('qty')}
                for item in (row.items or []) if isinstance(item, dict)
            ],
            'dueAtVenue': 0,
        })
    return base


def _verdict(code, message, booking=None, **extra):
    return {
        'code': code,
        'valid': code in (VALID, CHECKED_IN),
        'canCheckIn': code == VALID,
        'title': _TITLES[code],
        'message': message,
        'booking': booking,
        **extra,
    }


def _status_message(code, row):
    if code == PAYMENT_INCOMPLETE:
        return 'This booking has not been paid, so it is not a valid ticket.'
    if code == CANCELLED:
        return 'This booking was cancelled and is not valid for entry.'
    if code == REFUNDED:
        if row.status == 'refund_pending':
            return 'This booking is being refunded and is not valid for entry.'
        return 'This booking was refunded and is not valid for entry.'
    return f'This booking cannot be used for entry (status: {row.status}).'


def _checked_in_message(row):
    at = _ist(row.checked_in_at)
    return f'This booking was already checked in on {at:%d %b} at {at:%I:%M %p}.'


def evaluate(kind, row, *, today=None):
    """Requirements 4–7 for a booking already known to be for this gate:
    (code, message). Shared verbatim by the vendor and admin paths."""
    today = today or today_ist()
    code = _STATUS_VERDICT.get(row.status)
    if row.status != 'confirmed':
        code = code or INELIGIBLE
        return code, _status_message(code, row)
    if row.checked_in_at is not None:
        return ALREADY_CHECKED_IN, _checked_in_message(row)
    if row.date != today:
        when = 'was' if row.date < today else 'is'
        return WRONG_DATE, f'This booking {when} for {_date_label(row.date)}, not today.'
    if kind == 'venue':
        due = cash_due(row)
        if due:
            return VALID, f'Confirmed for today. Collect ₹{due:,} at the venue.'
    return VALID, 'Confirmed and paid for today.'


# --- Vendor -----------------------------------------------------------------

def _vendor_verdict(user, raw_id, gate, kind, row, reason):
    if row is None:
        if reason == SHORT_REF:
            return _verdict(
                SHORT_REF,
                'That looks like the short reference printed on an older invoice, '
                'not a Booking ID. Ask the customer for the Booking ID shown on '
                'their confirmation or in My bookings.',
            )
        return _verdict(INVALID_ID, 'No booking matches this ID. Check it with the customer.')
    gate_name = _gate_name(gate)
    if not _vendor_manages(user, kind, row):
        # Someone else's booking: say it is not valid HERE, and nothing else.
        return _verdict(WRONG_VENUE, f'This booking is not for {gate_name}.')
    if not _matches_gate(kind, row, gate):
        booked_for = _booked_for(kind, row)
        return _verdict(
            WRONG_VENUE,
            f'This booking is not for {gate_name}'
            + (f' — it is for {booked_for}.' if booked_for else '.'),
            bookedFor=booked_for or None,
        )
    code, message = evaluate(kind, row)
    return _verdict(code, message, gate_details(kind, row))


def _log(action, user, raw_id, gate, verdict, actor=None):
    # Booking IDs and account ids only — no customer names or phone numbers.
    gate_kind, gate_row = gate
    logger.info(
        'checkin.%s user=%s actor=%s gate=%s:%s booking=%r code=%s',
        action, user.pk, actor or '-', gate_kind, gate_row.pk,
        str(raw_id)[:MAX_INPUT], verdict['code'],
    )


def vendor_verify(user, raw_id, gate, *, actor=None):
    """Read-only verdict for a vendor at `gate`. Never writes."""
    kind, found = find_booking(raw_id)
    if kind is None:
        verdict = _vendor_verdict(user, raw_id, gate, None, None, found)
    else:
        verdict = _vendor_verdict(user, raw_id, gate, kind, found, None)
    _log('verify', user, raw_id, gate, verdict, actor)
    return verdict


def vendor_check_in(user, raw_id, gate, *, actor=None):
    """Verify, then record entry ONCE. Returns the verdict; code CHECKED_IN
    means this call admitted the booking.

    The booking row is locked (SELECT … FOR UPDATE) for the whole re-check,
    and the write is a conditional UPDATE that only matches a still-confirmed,
    not-yet-checked-in row. Either guard alone stops a double check-in; both
    together also hold on SQLite, which ignores FOR UPDATE.
    """
    from events.models import EventOrder

    kind, found = find_booking(raw_id)
    if kind is None:
        verdict = _vendor_verdict(user, raw_id, gate, None, None, found)
        _log('check_in', user, raw_id, gate, verdict, actor)
        return verdict

    model = Booking if kind == 'venue' else EventOrder
    with transaction.atomic():
        row = model.objects.select_for_update().get(pk=found.pk)
        verdict = _vendor_verdict(user, raw_id, gate, kind, row, None)
        if verdict['code'] == VALID:
            now = timezone.now()
            admitted = model.objects.filter(
                pk=row.pk, status='confirmed', checked_in_at__isnull=True,
            ).update(checked_in_at=now, checked_in_by=user)
            if admitted == 1:
                row.checked_in_at, row.checked_in_by = now, user
                at = _ist(now)
                verdict = _verdict(
                    CHECKED_IN, f'Entry recorded at {at:%I:%M %p}.', gate_details(kind, row),
                )
            else:
                # Lost a race we could not see under the lock (SQLite):
                # re-read and report what actually happened.
                row.refresh_from_db()
                verdict = _vendor_verdict(user, raw_id, gate, kind, row, None)
    _log('check_in', user, raw_id, gate, verdict, actor)
    return verdict


# --- Super admin --------------------------------------------------------------

def admin_verify(raw_id, *, admin=None):
    """Everything the support team needs about ANY booking ID — and whether it
    would be admitted if presented at its own venue today. READ-ONLY: a super
    admin investigates; entry is only ever recorded by the venue's vendor."""
    kind, row = find_booking(raw_id)
    logger.info(
        'checkin.admin_verify admin=%s booking=%r found=%s',
        getattr(admin, 'pk', '-'), str(raw_id)[:MAX_INPUT], kind or 'no',
    )
    if kind is None:
        if row == SHORT_REF:
            return {
                'found': False, 'code': SHORT_REF, 'title': _TITLES[SHORT_REF],
                'message': 'That is an older invoice reference (all hex), which is not '
                           'unique. Search the Booking ID shown on the confirmation or '
                           'in My bookings instead.',
            }
        return {
            'found': False, 'code': INVALID_ID, 'title': _TITLES[INVALID_ID],
            'message': 'No venue booking or event ticket has this ID.',
        }

    code, message = evaluate(kind, row)
    record = {
        'kind': kind,
        'id': row.id,
        'status': row.status,
        'displayStatus': row.display_status,
        'date': row.date.isoformat(),
        'createdAt': row.created_at.isoformat(),
        'customerName': row.customer_name or None,
        'phone': row.phone or None,
        'amount': row.amount,
        'fee': row.fee,
        'paymentId': row.razorpay_payment_id or None,
        'refundAmount': row.refund_amount,
        'refundReason': row.refund_reason or None,
        'checkedInAt': row.checked_in_at.isoformat() if row.checked_in_at else None,
        'checkedInBy': (
            {'name': _person(row.checked_in_by), 'phone': row.checked_in_by.phone or None}
            if row.checked_in_by_id else None
        ),
    }
    if kind == 'venue':
        vendor = row.listing.vendor if row.listing_id else None
        record.update({
            'venueName': row.venue_name,
            'venueId': str(row.listing_id) if row.listing_id else None,
            'unitLabel': row.unit_label or None,
            'slots': list(row.slots or []),
            'method': row.method,
            'walkIn': bool(row.walk_in),
            'paidOnline': row.online_amount if row.method not in (
                Booking.Method.VENUE, Booking.Method.WALK_IN) else 0,
            'dueAtVenue': cash_due(row),
            'vendor': {'name': vendor.name or None, 'phone': vendor.phone or None} if vendor else None,
        })
    else:
        event = row.event if row.event_id else None
        record.update({
            'eventName': row.event_name,
            'eventId': str(row.event_id) if row.event_id else None,
            'eventStatus': event.status if event else None,
            'venueName': event.venue_name if event else None,
            'organizer': event.organizer if event else None,
            'startTime': row.start_time.strftime('%H:%M') if row.start_time else None,
            'endTime': row.end_time.strftime('%H:%M') if row.end_time else None,
            'tickets': row.ticket_count,
            'admits': row.seats,
            'items': [
                {'label': item.get('label'), 'qty': item.get('qty')}
                for item in (row.items or []) if isinstance(item, dict)
            ],
            'paidOnline': row.amount if row.status in ('confirmed', 'refund_pending', 'refunded') else 0,
        })
    return {
        'found': True,
        'code': code,
        'title': _TITLES[code],
        'message': message,
        # Would the right vendor be allowed to admit it today? Advisory only.
        'eligibleToday': code == VALID,
        'booking': record,
    }


def checkin_today_count(user):
    """How many of the vendor's bookings were checked in today (IST) — the
    small counter on the portal's check-in screen."""
    from events.models import EventOrder
    from events.vendor_views import visible_events

    start = datetime.datetime.combine(today_ist(), datetime.time.min, tzinfo=IST)
    venue = Booking.objects.filter(
        listing__vendor=user, checked_in_at__gte=start,
    ).count()
    event = EventOrder.objects.filter(
        event__in=visible_events(user), checked_in_at__gte=start,
    ).count()
    return venue + event
