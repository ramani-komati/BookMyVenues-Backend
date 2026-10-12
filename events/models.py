"""
Event ticketing models.

Deliberately SEPARATE from bookings.Booking. A venue booking is exclusive —
one customer takes a slot and `overlaps()` locks everyone else out. An event
is the opposite: many customers buy into the SAME date and time until the
ticket capacity runs out. Bolting that onto Booking would mean teaching the
overlap check to sometimes not apply, which is exactly the kind of change
that breaks venue bookings. So events get their own tables and their own
endpoints, and venue booking logic is untouched.

Money is whole rupees (PositiveIntegerField) throughout — the same convention
Booking.amount uses, so the Razorpay paise conversion is identical.
"""
import uuid

from django.conf import settings
from django.db import models

from .catalog import canonical_event_category


def make_order_id():
    """Contract-style ids, e.g. 'eo_3f8a92c1d04e' (cf. bookings' 'bk_…')."""
    return 'eo_' + uuid.uuid4().hex[:12]


class Event(models.Model):
    """One ticketed event, e.g. 'ABC Dandiya Night'.

    OWNERSHIP: events are a SUPER-ADMIN product. The admin creates, edits,
    publishes and retires them from the admin panel; the organiser is
    recorded as plain text (`organizer_name` / `organizer_phone`) because the
    client handles organisers directly and an event must not require vendor
    onboarding, bank details or a payout account to exist.

    `vendor` is kept, nullable, for the events vendors created before that
    change — their orders, tickets and the vendor's read-only view all still
    resolve. Nothing new is ever written with a vendor.

    An event has one daily start/end time and one or more DATES it runs on
    (EventDate). Capacity is per date, because selling out Friday must not
    close Saturday.
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'            # admin still editing, never public
        PENDING = 'pending', 'Pending'      # LEGACY: vendor submission awaiting review
        LIVE = 'live', 'Live'               # PUBLISHED — visible and bookable
        PAUSED = 'paused', 'Paused'         # UNPUBLISHED — hidden, sold tickets stay valid
        REJECTED = 'rejected', 'Rejected'   # LEGACY: vendor submission refused
        DELETED = 'deleted', 'Deleted'      # soft-deleted; orders keep working

    # Statuses a customer may buy a ticket for. Everything else is invisible
    # to the public catalogue AND refused at order time.
    BOOKABLE_STATUSES = (Status.LIVE,)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # LEGACY only — see the class docstring. Null on every admin-created
    # event. SET_NULL, not CASCADE: deleting a vendor account must never
    # delete an event customers hold paid tickets for.
    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='events',
    )
    # The admin who created the event. Audit trail only — never a permission.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='created_events',
    )
    # The organiser as the admin recorded them. Display/contact text, NOT an
    # account: no login, no bank details, no payout is implied by these.
    organizer_name = models.CharField(max_length=150, blank=True, default='')
    organizer_phone = models.CharField(max_length=10, blank=True, default='')

    slug = models.SlugField(max_length=220, unique=True)

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    # Navaratri / Dandiya / … — see events/catalog.py. Indexed: the customer
    # Events page filters on it.
    category = models.CharField(max_length=50, blank=True, default='', db_index=True)

    poster_url = models.URLField(max_length=500, blank=True, default='')
    gallery = models.JSONField(default=list, blank=True)   # [url, …]

    # Where it happens. An event MAY point at one of the vendor's own live
    # venues (then the address is inherited for display), but it does not have
    # to — Dandiya grounds are often not a listed venue.
    listing = models.ForeignKey(
        'venues.Listing', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='events',
    )
    venue_name = models.CharField(max_length=200, blank=True, default='')
    address = models.CharField(max_length=300, blank=True, default='')
    locality = models.CharField(max_length=120, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    maps_link = models.URLField(max_length=500, blank=True, default='')

    # Daily show time, applied to every date (an event that runs 6–10 PM runs
    # 6–10 PM on each of its nights).
    start_time = models.TimeField()
    end_time = models.TimeField()

    # The selling window. Null = open-ended on that side. Enforced server-side
    # at order time, never by the client.
    booking_opens_on = models.DateField(null=True, blank=True)
    booking_closes_on = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    review_notes = models.TextField(blank=True, default='')

    contact_phone = models.CharField(max_length=10, blank=True, default='')
    terms = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['vendor']),
        ]

    def save(self, *args, **kwargs):
        self.category = canonical_event_category(self.category)
        super().save(*args, **kwargs)

    @property
    def organizer(self):
        """Who is running it, for admin/vendor display.

        The admin-entered organiser name wins; a legacy vendor-created event
        falls back to the vendor account it came from so those rows keep
        reading correctly in the panel.
        """
        if self.organizer_name:
            return self.organizer_name
        if self.vendor_id:
            return self.vendor.name or self.vendor.phone
        return ''

    @property
    def location(self):
        """The one-line place string the cards show."""
        parts = [p for p in (self.venue_name, self.locality, self.city) if p]
        return ', '.join(parts)

    def __str__(self):
        return f'{self.name} [{self.status}]'


class EventDate(models.Model):
    """One night of an event, with its OWN ticket capacity.

    Capacity lives here rather than on Event because selling out one night
    must not close the others, and because it is the row the order flow locks
    (select_for_update) to make overselling impossible.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='dates')
    date = models.DateField()
    capacity = models.PositiveIntegerField()
    # The vendor can close one night (bad weather) without deleting it —
    # existing orders for it stay valid.
    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ['date']
        constraints = [
            models.UniqueConstraint(fields=['event', 'date'], name='unique_event_date'),
        ]
        indexes = [models.Index(fields=['event', 'date'])]

    def __str__(self):
        return f'{self.event_id} on {self.date}'


class TicketType(models.Model):
    """A priced ticket class for one event: Adult, Child, VIP, Couple…

    `adult` and `child` are required by the product (every event quotes both);
    anything else is optional and vendor-defined. Prices are whole rupees.
    """

    # The two every event must carry — validated at create/update time.
    ADULT = 'adult'
    CHILD = 'child'
    REQUIRED_KEYS = (ADULT, CHILD)

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name='ticket_types')
    key = models.SlugField(max_length=40)          # 'adult', 'vip', 'couple'…
    label = models.CharField(max_length=80)        # 'Adult', 'VIP', 'Couple'
    price = models.PositiveIntegerField()          # ₹, whole rupees
    # How many of the date's capacity units ONE ticket of this type consumes.
    # 1 for adult/child; a Couple ticket admitting two people is 2, so capacity
    # cannot be oversold by selling couples.
    seats_per_ticket = models.PositiveSmallIntegerField(default=1)
    # 0 = no per-order cap beyond the global MAX_TICKETS_PER_ORDER.
    max_per_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['event', 'key'], name='unique_event_ticket_type'
            ),
        ]

    def as_record(self):
        return {
            'key': self.key,
            'label': self.label,
            'price': self.price,
            'seatsPerTicket': self.seats_per_ticket,
            'maxPerOrder': self.max_per_order or None,
            'isActive': self.is_active,
        }

    def __str__(self):
        return f'{self.label} — ₹{self.price} ({self.event_id})'


class EventOrder(models.Model):
    """One customer's ticket purchase for ONE date of one event.

    Kept distinct from bookings.Booking on purpose: the vendor dashboard,
    payouts and the availability maths all ask different questions of it, and
    sharing a table would have meant teaching every one of those code paths
    about a row type it has no business seeing.
    """

    # Same lifecycle vocabulary as Booking.status, so the admin panel and the
    # Razorpay webhook treat both kinds of row identically.
    DEAD_STATUSES = ('cancelled', 'refunded')

    id = models.CharField(
        max_length=20, primary_key=True, default=make_order_id, editable=False
    )
    # SET_NULL mirrors Booking.listing: deleting an event must never erase a
    # customer's ticket history — the denormalized fields keep it rendering.
    event = models.ForeignKey(
        Event, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders'
    )
    event_date = models.ForeignKey(
        EventDate, null=True, blank=True, on_delete=models.SET_NULL, related_name='orders'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='event_orders',
    )

    # Frozen at purchase time so the ticket still reads correctly after the
    # vendor edits (or deletes) the event.
    event_name = models.CharField(max_length=200, blank=True, default='')
    event_category = models.CharField(max_length=50, blank=True, default='')
    location = models.CharField(max_length=250, blank=True, default='')
    image = models.URLField(max_length=500, blank=True, default='')
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    customer_name = models.CharField(max_length=150, blank=True, default='')
    phone = models.CharField(max_length=10, blank=True, default='')

    # [{key, label, qty, price, seatsPerTicket, subtotal}] — priced from the
    # EVENT's ticket types, never from the request body.
    items = models.JSONField(default=list)
    # Capacity units this order consumes = Σ(qty × seatsPerTicket). Stored so
    # the oversell check is one SUM() under the row lock instead of a walk
    # over every order's JSON.
    seats = models.PositiveIntegerField(default=0)
    # Ticket COUNT (Σ qty) — what the customer sees ("5 tickets").
    ticket_count = models.PositiveIntegerField(default=0)

    subtotal = models.PositiveIntegerField(default=0)   # before the fee
    fee = models.PositiveIntegerField(default=0)        # frozen platform fee
    amount = models.PositiveIntegerField(default=0)     # subtotal + fee

    status = models.CharField(max_length=20, default='payment_pending')

    razorpay_order_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True, default='')
    refund_id = models.CharField(max_length=64, blank=True, default='')
    refund_reason = models.CharField(max_length=200, blank=True, default='')
    refund_amount = models.PositiveIntegerField(null=True, blank=True)

    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The oversell check always asks about one date.
            models.Index(fields=['event_date', 'status']),
            models.Index(fields=['user', 'date']),
            models.Index(fields=['event', 'date']),
        ]

    @property
    def display_status(self):
        """'confirmed' becomes 'completed' once the show has ended (IST) —
        the same derivation Booking.display_status does for slots."""
        from bookings.slots import now_minutes_ist, today_ist

        if self.status and self.status != 'confirmed':
            return self.status
        today = today_ist()
        if self.date < today:
            return 'completed'
        if self.date == today and self.end_time:
            end = self.end_time.hour * 60 + self.end_time.minute
            # 00:00 as an END means midnight — the show runs to day's end.
            if end == 0:
                end = 24 * 60
            if end <= now_minutes_ist():
                return 'completed'
        return 'confirmed'

    def as_record(self):
        """The camelCase record the frontend expects (cf. Booking.as_record)."""
        return {
            'id': self.id,
            'kind': 'event',          # lets shared UI tell tickets from bookings
            'status': self.display_status,
            'eventId': str(self.event_id) if self.event_id else None,
            'eventName': self.event_name,
            'eventCategory': self.event_category,
            'location': self.location,
            'image': self.image,
            'date': self.date.isoformat(),
            'startTime': self.start_time.strftime('%H:%M') if self.start_time else None,
            'endTime': self.end_time.strftime('%H:%M') if self.end_time else None,
            'customer': self.customer_name,
            'phone': self.phone or None,
            'items': self.items,
            'tickets': self.ticket_count,
            'seats': self.seats,
            'subtotal': self.subtotal,
            'fee': self.fee,
            'amount': self.amount,
            'createdAt': self.created_at.isoformat(),
        }

    def __str__(self):
        return f'{self.id} — {self.event_name} on {self.date}'
