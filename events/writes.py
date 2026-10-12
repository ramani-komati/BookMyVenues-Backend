"""
Event field parsing, validation and child-row syncing.

Extracted from events/vendor_views.py when events became a SUPER-ADMIN
product: the admin panel now owns create/edit, and the rules that protect
customers who already hold tickets (a date with sales cannot be removed or
shrunk, a sold ticket type is deactivated rather than deleted) must be the
SAME code whoever calls it. Two copies of those rules would eventually
disagree, and the disagreement would be discovered by a customer whose paid
ticket stopped resolving.

Every function raises SlotError with a message written for a human — the
views turn that into a 400 with the text unchanged, so the admin form shows
the real reason instead of "invalid input".
"""
import datetime
import uuid

from django.utils.text import slugify

from bookings.slots import SlotError, parse_date, today_ist
from venues.models import Listing

from .catalog import canonical_event_category
from .inventory import seats_sold
from .location import clean_maps_link
from .models import Event, EventDate, EventOrder, TicketType

MAX_DATES = 60          # a season-long Navaratri run is 9-10 nights; 60 is ample
MAX_TICKET_TYPES = 8
MAX_CAPACITY = 100_000
MAX_TICKET_PRICE = 1_000_000
MAX_GALLERY = 8


# --- Field parsing -------------------------------------------------------

def text(value, limit):
    return str(value or '').strip()[:limit]


def parse_time(value, field):
    """'18:00' -> time. Accepts 'HH:MM' and 'HH:MM:SS'."""
    raw = str(value or '').strip()
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    raise SlotError(f'{field} must look like HH:MM.')


def positive_int(value, field, maximum):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise SlotError(f'{field} must be a number.')
    if number < 0:
        raise SlotError(f'{field} cannot be negative.')
    if number > maximum:
        raise SlotError(f'{field} cannot exceed {maximum:,}.')
    return number


def parse_dates(raw, *, require=True, existing=()):
    """[{date, capacity, isAvailable}] -> normalised list.

    A NEW date in the past is refused: an event night that can never be
    booked is not something anyone means to add, and it would put a dead row
    in the customer's date picker.

    `existing` is the set of dates the event ALREADY runs on. Those are
    allowed through even once they are in the past — otherwise editing a
    currently-running event (fixing a typo on day 4 of a 9-night Navaratri)
    would be impossible without deleting the nights that already happened,
    which is exactly what sold tickets must survive.
    """
    if raw is None:
        if require:
            raise SlotError('At least one event date is required.')
        return None
    if not isinstance(raw, list) or not raw:
        raise SlotError('At least one event date is required.')
    if len(raw) > MAX_DATES:
        raise SlotError(f'At most {MAX_DATES} dates per event.')

    today = today_ist()
    existing = set(existing or ())
    seen, parsed = set(), []
    for entry in raw:
        if not isinstance(entry, dict):
            raise SlotError('Each date must be an object with date and capacity.')
        date = parse_date(entry.get('date'))
        if date in seen:
            raise SlotError(f'Duplicate date: {date.isoformat()}.')
        seen.add(date)
        capacity = positive_int(entry.get('capacity'), 'capacity', MAX_CAPACITY)
        if capacity == 0:
            raise SlotError('capacity must be at least 1.')
        if date < today and date not in existing:
            raise SlotError(
                f'{date.isoformat()} is in the past — a new event date must be '
                f'today or later.'
            )
        parsed.append({
            'date': date,
            'capacity': capacity,
            'is_available': entry.get('isAvailable', True) is not False,
        })
    return parsed


def parse_ticket_types(raw, *, require=True):
    """[{key,label,price,...}] -> normalised list. Adult and child required."""
    if raw is None:
        if require:
            raise SlotError('Adult and child ticket prices are required.')
        return None
    if not isinstance(raw, list) or not raw:
        raise SlotError('Adult and child ticket prices are required.')
    if len(raw) > MAX_TICKET_TYPES:
        raise SlotError(f'At most {MAX_TICKET_TYPES} ticket types per event.')

    seen, parsed = set(), []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SlotError('Each ticket type must be an object.')
        key = slugify(str(entry.get('key') or entry.get('label') or ''))[:40]
        if not key:
            raise SlotError('Each ticket type needs a key or a label.')
        if key in seen:
            raise SlotError(f'Duplicate ticket type: {key}.')
        seen.add(key)
        parsed.append({
            'key': key,
            'label': text(entry.get('label') or key.replace('-', ' ').title(), 80),
            'price': positive_int(entry.get('price'), f'{key} price', MAX_TICKET_PRICE),
            'seats_per_ticket': max(
                1, positive_int(entry.get('seatsPerTicket', 1), 'seatsPerTicket', 20)
            ),
            'max_per_order': positive_int(
                entry.get('maxPerOrder', 0) or 0, 'maxPerOrder', 100
            ),
            'is_active': entry.get('isActive', True) is not False,
            'sort_order': index,
        })

    missing = [k for k in TicketType.REQUIRED_KEYS if k not in seen]
    if missing:
        raise SlotError(
            'Every event needs an "{}" ticket price.'.format('" and "'.join(missing))
        )
    return parsed


def unique_slug(name, event_id):
    base = slugify(str(name or ''))[:200] or str(event_id)[:8]
    slug, counter = base, 2
    while Event.objects.filter(slug=slug).exclude(pk=event_id).exists():
        slug = f'{base}-{counter}'
        counter += 1
    return slug


def resolve_listing(raw, *, owner=None):
    """An optional link to a listed venue, so the address can be inherited.

    `owner` scopes the lookup: a vendor may only point at their OWN venue
    (the hole venues/views guards on `unitOf`). The admin passes owner=None
    and may link any existing listing, which is the whole point of the admin
    owning events — they are not restricted to one vendor's estate.
    Returns (listing, error_message).
    """
    ref = str(raw or '').strip()
    if not ref:
        return None, None
    try:
        listing = Listing.objects.filter(pk=uuid.UUID(ref)).first()
    except (ValueError, TypeError):
        listing = None
    if listing is None:
        return None, 'venueId must reference an existing venue.'
    if owner is not None and listing.vendor_id != owner.id:
        return None, 'venueId must reference one of your own venues.'
    return listing, None


def apply_core_fields(event, body, *, creating, listing_owner=None):
    """Write the scalar fields. Raises SlotError on bad input."""
    if creating or 'name' in body:
        name = text(body.get('name'), 200)
        if not name:
            raise SlotError('An event name is required.')
        event.name = name
    if creating or 'description' in body:
        event.description = text(body.get('description'), 5000)
    if creating or 'category' in body:
        category = canonical_event_category(body.get('category'))
        if creating and not category:
            raise SlotError('An event category is required (e.g. Navaratri).')
        event.category = category
    if creating or 'posterUrl' in body or 'poster' in body:
        event.poster_url = text(body.get('posterUrl') or body.get('poster'), 500)
    if 'gallery' in body:
        gallery = body.get('gallery')
        if not isinstance(gallery, list):
            raise SlotError('gallery must be a list of image URLs.')
        event.gallery = [text(url, 500) for url in gallery[:MAX_GALLERY] if url]

    for field, key, limit in (
        ('venue_name', 'venueName', 200),
        ('address', 'address', 300),
        ('locality', 'locality', 120),
        ('city', 'city', 100),
        ('maps_link', 'mapsLink', 500),
        ('contact_phone', 'contactPhone', 10),
        ('terms', 'terms', 3000),
        # Admin-recorded organiser. Plain text on purpose — see Event's docstring.
        ('organizer_name', 'organizerName', 150),
        ('organizer_phone', 'organizerPhone', 10),
    ):
        if creating or key in body:
            setattr(event, field, text(body.get(key), limit))

    # The customer page embeds and links this — only a real Google Maps URL.
    if creating or 'mapsLink' in body:
        event.maps_link = clean_maps_link(body.get('mapsLink'))

    if creating or 'startTime' in body:
        event.start_time = parse_time(body.get('startTime'), 'startTime')
    if creating or 'endTime' in body:
        event.end_time = parse_time(body.get('endTime'), 'endTime')

    for field, key in (
        ('booking_opens_on', 'bookingOpensOn'),
        ('booking_closes_on', 'bookingClosesOn'),
    ):
        if key in body:
            raw = body.get(key)
            setattr(event, field, parse_date(raw) if raw else None)

    if (
        event.booking_opens_on
        and event.booking_closes_on
        and event.booking_opens_on > event.booking_closes_on
    ):
        raise SlotError('bookingOpensOn cannot be after bookingClosesOn.')

    if 'venueId' in body:
        listing, error = resolve_listing(body.get('venueId'), owner=listing_owner)
        if error:
            raise SlotError(error)
        event.listing = listing
        if listing is not None and not event.venue_name:
            event.venue_name = listing.name

    # A place is required for a ticket to mean anything — the customer has to
    # know where to turn up. Either a linked venue or a typed venue name will
    # do; "to be announced" is not an event we are willing to sell.
    if creating and not (event.venue_name or event.listing_id):
        raise SlotError('A venue name or a linked venue is required.')


# --- Child-row syncing ---------------------------------------------------

def has_sold_tickets(event):
    """True when money has been taken for this event (unpaid holds excluded)."""
    return EventOrder.objects.filter(event=event).exclude(
        status__in=(*EventOrder.DEAD_STATUSES, 'payment_pending')
    ).exists()


def sync_dates(event, rows):
    """Replace the event's dates with `rows`.

    A date that already has sold tickets may have its capacity RAISED or its
    availability toggled, but it can neither be removed nor shrunk below what
    is already sold — that would invalidate tickets customers have paid for.
    Returns an error message, or None.
    """
    existing = {d.date: d for d in event.dates.all()}
    wanted = {row['date']: row for row in rows}

    for date, event_date in existing.items():
        if date in wanted:
            continue
        if seats_sold(event_date) > 0:
            return (
                f'{date.isoformat()} already has tickets sold and cannot be '
                f'removed. Mark it unavailable instead.'
            )

    for date, event_date in existing.items():
        if date not in wanted:
            event_date.delete()

    for date, row in wanted.items():
        event_date = existing.get(date)
        if event_date is None:
            EventDate.objects.create(event=event, **row)
            continue
        sold = seats_sold(event_date)
        if row['capacity'] < sold:
            return (
                f'{date.isoformat()} already has {sold} ticket(s) sold — '
                f'capacity cannot be set below that.'
            )
        event_date.capacity = row['capacity']
        event_date.is_available = row['is_available']
        event_date.save(update_fields=['capacity', 'is_available'])
    return None


def sync_ticket_types(event, rows):
    """Replace the event's ticket types.

    A type that has already been sold is DEACTIVATED rather than deleted, so
    existing orders keep rendering their line items with the right label.
    Prices are free to change — orders froze their own prices at purchase.
    """
    existing = {t.key: t for t in event.ticket_types.all()}
    wanted = {row['key']: row for row in rows}

    # Which keys appear on live orders. Walked in Python rather than with a
    # JSONField `contains` lookup, which SQLite does not support — the local
    # dev/test database would raise NotSupportedError on an otherwise fine
    # edit. One query, bounded by the event's own order count.
    sold_keys = set()
    for order_items in EventOrder.objects.filter(event=event).exclude(
        status__in=EventOrder.DEAD_STATUSES
    ).values_list('items', flat=True):
        for item in order_items or []:
            if isinstance(item, dict) and item.get('key'):
                sold_keys.add(item['key'])

    for key, ticket_type in existing.items():
        if key in wanted:
            continue
        if key in sold_keys:
            ticket_type.is_active = False
            ticket_type.save(update_fields=['is_active'])
        else:
            ticket_type.delete()

    for key, row in wanted.items():
        ticket_type = existing.get(key)
        if ticket_type is None:
            TicketType.objects.create(event=event, **row)
            continue
        for field, value in row.items():
            setattr(ticket_type, field, value)
        ticket_type.save()
