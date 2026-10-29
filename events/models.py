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

from bookings.booking_codes import ALPHABET, LENGTH
from bookings.payment_record import payment_time_fields

from .catalog import canonical_event_category


# Event order primary keys — the booking ID the customer reads out at the
# gate ("K7M2QX"). Six uppercase letters/digits, minus the look-alikes 0/O
# and 1/I/L.
# The shared, platform-wide booking ID format (bookings/booking_codes.py).
ORDER_ID_ALPHABET = ALPHABET
ORDER_ID_LENGTH = LENGTH
# Never confusable with an OLDER booking's reference: a legacy venue booking
# ('bk_…3f8a92') shows as "#3F8A92" on invoices and in the admin panel, and a
# new ID always contains a letter outside 0-9A-F, so it can never equal one.
# Orders created before short IDs keep their 15-character 'eo_…' ids.


def make_order_id():
    """A new event order's primary key: 6 random characters (never all-hex),
    from the OS's secure generator — the platform-wide booking ID format that
    venue bookings now share (bookings/booking_codes.py).

    Existing orders keep the ids they were created with ('eo_3f8a92c1d04e');
    only NEW orders get one of these. The function keeps its original name
    because the migrations reference it as the field default.
    """
    from bookings.booking_codes import new_code
    return new_code()


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
    # The event's map pin, DERIVED on every save: the exact pin in maps_link,
    # else the pin of the listed venue it is held at (the event already
    # inherits that venue's address for display). Never typed in or guessed —
    # null when neither carries an exact pin. See venues/geo.py.
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

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

    # Homepage promotion, set by the super admin. Deliberately SEPARATE from
    # `status`: publishing makes an event visible, promoting gives a published
    # one featured placement. Only a LIVE event can carry it — save() clears
    # it whenever the event leaves LIVE, so an unpublished, rejected or
    # deleted event can never be promoted publicly. The cap is enforced by
    # the admin endpoint (MAX_FEATURED_EVENTS).
    featured = models.BooleanField(default=False)
    # When it was promoted — orders the featured row (newest promotion first).
    featured_at = models.DateTimeField(null=True, blank=True)
    # Super admin's Homepage page (adminpanel/homepage.py). All three are
    # OPTIONAL and only refine an existing promotion: `featured_order` is the
    # admin's position in the Top Events row (lower first; null = after the
    # ordered ones, newest promotion first as before), and the from/until
    # window (IST dates, inclusive) schedules when a promoted event appears.
    # A null window side is open-ended, so every event promoted before these
    # existed keeps showing exactly as it did.
    featured_order = models.PositiveIntegerField(null=True, blank=True)
    featured_from = models.DateField(null=True, blank=True)
    featured_until = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['vendor']),
        ]

    # How many events may be promoted at once. Enforced by the admin
    # endpoint; the customer page simply renders whatever is promoted.
    # Raised from 6 when promotions became schedulable: an admin lining up
    # next month's events must not have to unpromote this week's first. How
    # many the homepage actually SHOWS is a separate, admin-set display limit
    # (adminpanel/homepage.py, Top Events section), 6 by default.
    MAX_FEATURED = 12

    def sync_coordinates(self):
        """Set latitude/longitude from maps_link, else the linked venue."""
        from venues.geo import pin_from_maps_link

        point = pin_from_maps_link(self.maps_link)
        if point is None and self.listing_id:
            from venues.models import Listing
            venue = (
                Listing.objects.filter(pk=self.listing_id)
                .values_list('latitude', 'longitude')
                .first()
            )
            if venue and venue[0] is not None and venue[1] is not None:
                point = venue
        self.latitude, self.longitude = point if point else (None, None)

    def save(self, *args, **kwargs):
        self.category = canonical_event_category(self.category)
        self.sync_coordinates()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = {*update_fields, 'latitude', 'longitude'}
        if self.featured and self.status != self.Status.LIVE:
            self.featured = False
            self.featured_at = None
            # The schedule belongs to the promotion and goes with it, so a
            # re-promotion later starts from a clean slate.
            self.featured_order = None
            self.featured_from = None
            self.featured_until = None
            if update_fields is not None:
                update_fields |= {
                    'featured', 'featured_at', 'featured_order',
                    'featured_from', 'featured_until',
                }
        if update_fields is not None:
            kwargs['update_fields'] = update_fields
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
        """The one-line place string the cards show (venue, locality, city)."""
        from .location import short_location
        return short_location(self)

    @property
    def full_address(self):
        """Venue, street address, locality and city, without repeats."""
        from .location import full_address
        return full_address(self)

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

    # Lifecycle (status is a plain CharField — no migration for new values):
    #   payment_pending → confirmed                  verified payment only
    #   payment_pending → failed                     Razorpay payment.failed
    #   payment_pending → cancelled                  customer dismissed checkout
    #   failed          → confirmed                  a RETRY in the same checkout
    #                                                 succeeded (verified)
    #   confirmed       → refund_pending → refunded  refund workflow
    #   unpaid          → removed                    super-admin soft delete
    # DEAD rows hold no seats. 'failed' and 'removed' are dead too, otherwise
    # an abandoned attempt would keep its seats off sale for ever.
    DEAD_STATUSES = ('cancelled', 'refunded', 'failed', 'removed')
    # Money was captured and has not (yet) gone back — never removable.
    PAID_STATUSES = ('confirmed', 'refund_pending')
    # Unpaid outcomes a super admin may soft-remove.
    REMOVABLE_STATUSES = ('payment_pending', 'failed', 'cancelled')

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
    # Entry at the gate — the WHOLE order (every ticket on it) enters at once:
    # tickets have no IDs of their own, so they cannot be admitted one by one.
    # Written once by bookings/checkin.py (row lock + conditional UPDATE).
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='checked_in_event_orders',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Same meaning and rules as on bookings.Booking — ONE invoice series for
    # both products (bookings/invoices.py), issued once on insert, never
    # changed, and no proof of payment. Nullable only for the live-table
    # migration; the bookings 0022 data migration numbered existing orders.
    invoice_number = models.CharField(
        max_length=24, unique=True, null=True, blank=True, editable=False
    )
    # When the successful payment happened, and where that time came from
    # (bookings/payment_record.py). Never the creation time.
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_at_source = models.CharField(max_length=10, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # The oversell check always asks about one date.
            models.Index(fields=['event_date', 'status']),
            models.Index(fields=['user', 'date']),
            models.Index(fields=['event', 'date']),
        ]

    def __init__(self, *args, **kwargs):
        # Rows loaded from the database arrive positionally (Model.from_db);
        # only a brand-new order built without an explicit id gets a
        # GENERATED one — the only kind save() may swap on a clash.
        generated = not args and 'id' not in kwargs and 'pk' not in kwargs
        super().__init__(*args, **kwargs)
        self._generated_id = generated

    def save(self, *args, **kwargs):
        """Insert a new order under a collision-safe short id.

        The id must be unique across ALL booking types — venue bookings use
        the same 6-character format — so the insert goes through
        bookings.booking_codes.insert_with_code: an id in use anywhere is
        skipped, and the order row is written in one savepoint with its
        BookingCode registry row, whose primary key makes a cross-table clash
        impossible (a clash is retried with a fresh id). An explicit id, an
        update of an existing order, and any other integrity error behave
        exactly as Django normally does.

        The new order also gets its invoice number in the same transaction
        (bookings/invoices.py); an existing one is never replaced.
        """
        if not self._state.adding:
            return super().save(*args, **kwargs)
        from bookings.booking_codes import insert_with_code
        from bookings.invoices import insert_with_invoice
        return insert_with_invoice(self, lambda: insert_with_code(
            self, 'event', lambda: super(EventOrder, self).save(*args, **kwargs)
        ))

    @property
    def hold_expired(self):
        """An unpaid order whose seat hold has lapsed. Still payment_pending
        in the database (a late verified capture can confirm it), but nobody
        should be told it is still "awaiting payment"."""
        import datetime

        from django.utils import timezone

        from bookings.views import PENDING_HOLD_MINUTES

        if self.status != 'payment_pending' or self.created_at is None:
            return False
        cutoff = timezone.now() - datetime.timedelta(minutes=PENDING_HOLD_MINUTES)
        return self.created_at < cutoff

    @property
    def display_status(self):
        """'confirmed' becomes 'completed' once the show has ended (IST) —
        the same derivation Booking.display_status does for slots — and an
        unpaid order whose hold lapsed reads 'expired'."""
        from bookings.slots import now_minutes_ist, today_ist

        if self.hold_expired:
            return 'expired'
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
            # The booking ID itself — 6 characters for new orders, the original
            # 'eo_…' id for older ones. Shown to customers, the gate and the
            # admin, and the key for every API call.
            'id': self.id,
            'kind': 'event',          # lets shared UI tell tickets from bookings
            'status': self.display_status,
            'eventId': str(self.event_id) if self.event_id else None,
            'eventName': self.event_name,
            'eventCategory': self.event_category,
            'location': self.location,
            # The full place for the ticket: the live event's venue, address,
            # locality and city when the event still exists (orders bought
            # before addresses were captured still read fully), else the
            # place frozen on the order at purchase.
            'address': (self.event.full_address if self.event_id and self.event else '')
            or self.location,
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
            'checkedInAt': self.checked_in_at.isoformat() if self.checked_in_at else None,
            'createdAt': self.created_at.isoformat(),
            # Booking Info only — served to the order's own customer, the
            # event's vendor and the admin, never on a public endpoint.
            'invoiceNumber': self.invoice_number or None,
            **payment_time_fields(self),
        }

    def rating_fields(self):
        """Rating keys for the CUSTOMER's ticket history — the same contract
        as Booking.rating_fields: the SERVER decides when a ticket may be
        reviewed (after the show, verified-paid only)."""
        return {
            'ratingStatus': self.rating_status,
            'userRating': self.user_rating,
            'userReview': self.user_review,
        }

    @property
    def user_rating(self):
        """The customer's own stars for this ticket, or None (select_related
        'rating' when listing many orders)."""
        rating = getattr(self, 'rating', None)
        return rating.stars if rating else None

    @property
    def user_review(self):
        rating = getattr(self, 'rating', None)
        return (rating.review or None) if rating else None

    @property
    def rating_status(self):
        """See bookings.ratings.order_rating_status."""
        from bookings.ratings import order_rating_status
        return order_rating_status(self)

    def __str__(self):
        return f'{self.id} — {self.event_name} on {self.date}'
