"""
Booking model — one confirmed reservation of time slots at a venue.
"""
from django.conf import settings
from django.db import models

from .booking_codes import insert_with_code, new_code
from .invoices import insert_with_invoice
from .payment_record import payment_time_fields


def make_booking_id():
    """A new venue booking's ID: the platform-wide 6-character booking ID
    ("K7M2QX") that event tickets use too — see bookings/booking_codes.py.

    Bookings made before this keep their original 'bk_3f8a92c1d04e' IDs. The
    function keeps its name because the migrations reference it as the
    field default."""
    return new_code()


class Booking(models.Model):
    class Method(models.TextChoices):
        ONLINE = 'online', 'Online'          # generic online payment
        UPI = 'upi', 'UPI'
        CARD = 'card', 'Card'
        NETBANKING = 'netbanking', 'Netbanking'
        VENUE = 'venue', 'Pay at venue'      # RETIRED — kept for old rows only
        WALK_IN = 'walk-in', 'Walk-in'       # vendor-recorded offline booking

    # Methods a customer may send on POST /users/me/bookings. Everything except
    # 'venue' means the money was collected online (payout passes it through).
    # What a CUSTOMER may choose today. Pay-at-venue was retired: every
    # customer booking now goes through Razorpay. 'venue' stays in Method
    # above because existing rows still carry it (history, payouts, the
    # vendor's cash-collected flag) — it just cannot be created any more.
    # 'walk-in' is vendor-only (see WalkInBookingView), never customer-chosen.
    CUSTOMER_METHODS = {'online', 'upi', 'card', 'netbanking'}

    id = models.CharField(
        max_length=20, primary_key=True, default=make_booking_id, editable=False
    )
    # SET_NULL: deleting a venue must NOT erase customers' booking
    # history — the denormalized venue_name/image fields keep the
    # booking cards rendering.
    listing = models.ForeignKey(
        'venues.Listing',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bookings',
    )
    # Null for walk-ins (vendor books on behalf of an offline customer).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='bookings',
    )

    # Copied from the listing at booking time, so the customer's booking
    # card still shows correctly even if the listing changes later.
    venue_name = models.CharField(max_length=200, blank=True, default='')
    category = models.CharField(max_length=50, blank=True, default='')
    location = models.CharField(max_length=250, blank=True, default='')
    image = models.URLField(max_length=500, blank=True, default='')

    customer_name = models.CharField(max_length=150, blank=True, default='')
    phone = models.CharField(max_length=10, blank=True, default='')

    # Per-unit booking (P6): a venue can have several pitches/courts/screens.
    # sport is '' for non-playzone venues; unit is 1-based; unit=None means a
    # legacy / whole-venue booking that blocks every unit (safe default).
    sport = models.CharField(max_length=100, blank=True, default='')
    unit = models.PositiveSmallIntegerField(null=True, blank=True)
    unit_label = models.CharField(max_length=150, blank=True, default='')

    # What was bought. '' = an ordinary slot booking (every row made before
    # this existed); 'membership' | 'package' | 'surprise' = a gym
    # membership, a resort package or a big screen surprise, whose own
    # details live in offerings/models.py. Those kinds never take part in
    # the slot overlap checks (see views._active_bookings) — they have
    # capacity rules of their own (offerings/availability.py).
    kind = models.CharField(max_length=12, blank=True, default='')

    date = models.DateField()
    slots = models.JSONField(default=list)      # ["19:30 – 21:00", ...]
    per_slot = models.PositiveIntegerField(default=0)  # hourly rate (₹)
    addons = models.JSONField(default=list)     # [{"name", "qty", "price"}]
    # Applied coupon/offer (server-validated) + the discount it gave, in ₹.
    offer = models.JSONField(null=True, blank=True)  # {code,title,type,value} or null
    discount_amount = models.PositiveIntegerField(default=0)
    # The platform fee that was ACTUALLY charged on this booking (₹). Frozen at
    # creation so later fee changes never corrupt payout math for old bookings
    # (payout = Σ(amount − fee)). 0 for walk-ins (no fee).
    fee = models.PositiveIntegerField(default=20)
    amount = models.PositiveIntegerField()      # total, server-computed (₹)

    method = models.CharField(max_length=10, choices=Method.choices, default=Method.ONLINE)
    walk_in = models.BooleanField(default=False)
    # DISPLAY ONLY — the vendor ticking "cash collected" for a pay-at-venue or
    # walk-in booking. Deliberately has no effect on amounts, earnings, stats
    # or payout math; it exists so a vendor can track who actually paid at the
    # door (and spot no-shows).
    collected = models.BooleanField(default=False)
    collected_at = models.DateTimeField(null=True, blank=True)
    # Lifecycle state (plain CharField — no migration for new values):
    #   payment_pending → confirmed               verified payment only
    #   payment_pending → failed                  Razorpay payment.failed
    #   payment_pending → cancelled               customer dismissed checkout
    #   failed          → confirmed               a RETRY in the same checkout
    #                                              succeeded (verified)
    #   confirmed       → refund_pending → refunded
    #   unpaid          → removed                 super-admin soft delete
    # 'payment_pending' holds the slot until hold_expires_at (5 minutes —
    # bookings/holds.py) or until paid; 'completed' is derived from the date
    # (display_status).
    status = models.CharField(max_length=20, default='confirmed')
    # Razorpay linkage (empty for pay-at-venue / walk-ins / legacy bookings).
    # Part payment: what was charged online vs owed in cash at the venue.
    # pay_now is NULL for every booking taken before the feature existed,
    # which means "the whole amount was paid online" — see online_amount.
    # Deliberately not backfilled: a nullable column degrades safely, whereas
    # a botched backfill would corrupt payout maths for real money.
    pay_now = models.PositiveIntegerField(null=True, blank=True)
    at_venue = models.PositiveIntegerField(default=0)
    part_payment = models.JSONField(null=True, blank=True)

    # What the booking is for, and anything the customer wants the venue to
    # know ("Surprise — cake at 8"). Both optional and free text; they exist
    # so the VENDOR sees them, so they are echoed on every booking record.
    occasion = models.CharField(max_length=80, blank=True, default='')
    occasion_note = models.CharField(max_length=500, blank=True, default='')

    # Set once the confirmation actually got through, so a webhook
    # re-delivery never sends a second copy.
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    razorpay_order_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True, default='')
    refund_id = models.CharField(max_length=64, blank=True, default='')
    # Set by the admin Refunds panel when issuing a refund.
    refund_reason = models.CharField(max_length=200, blank=True, default='')
    refund_amount = models.PositiveIntegerField(null=True, blank=True)
    # Entry at the venue. Written ONCE, by bookings/checkin.py, under a row
    # lock plus a conditional UPDATE — so two simultaneous check-ins can never
    # both succeed. checked_in_by is the vendor account that admitted the
    # customer; SET_NULL keeps the timestamp if that account is ever deleted.
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='checked_in_bookings',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # When an unpaid ('payment_pending') row stops holding its slot: set ONCE,
    # from the server clock, when the hold is created (bookings/holds.py) and
    # never extended. Null on rows from before holds were timed this way —
    # those fall back to created_at + HOLD_MINUTES.
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    # The booking's invoice number ("INV-2627-000123"): issued ONCE by
    # bookings/invoices.py when the row is first inserted, from the series
    # event orders share, and never changed. It says the booking exists — NOT
    # that it was paid (that is status + paid_at). Nullable only so the
    # column could be added to the live table; the 0022 data migration
    # numbered every existing row.
    invoice_number = models.CharField(
        max_length=24, unique=True, null=True, blank=True, editable=False
    )
    # When the SUCCESSFUL payment happened — never the creation time — and
    # where that time came from ('gateway' | 'checkout' | 'webhook'). See
    # bookings/payment_record.py. Empty for walk-ins, pay-at-venue, unpaid
    # rows and bookings paid before this was recorded.
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_at_source = models.CharField(max_length=10, blank=True, default='')

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Overlap checks always query one venue on one date.
            models.Index(fields=['listing', 'date']),
            models.Index(fields=['user', 'date']),
        ]

    # Rows that neither hold a slot nor count as revenue. Availability and
    # the vendor dashboard both exclude these, so they can never disagree
    # about whether a booking "exists".
    # 'failed' and 'removed' are dead too, otherwise a failed attempt or an
    # admin-removed row would keep its slot off sale for ever.
    DEAD_STATUSES = ('cancelled', 'refunded', 'failed', 'removed')
    # Statuses a VERIFIED payment may still confirm. 'failed' is included on
    # purpose: Razorpay keeps an order payable after a failed attempt, so a
    # retry inside the same checkout can succeed (cf. events.payments).
    CONFIRMABLE_STATUSES = ('payment_pending', 'failed')
    # Unpaid outcomes a super admin may soft-remove — the same set events use
    # (EventOrder.REMOVABLE_STATUSES). Never confirmed / refund_pending /
    # refunded: those rows are financial records. See
    # adminpanel.formatters.booking_removal_block for the full rule.
    REMOVABLE_STATUSES = ('payment_pending', 'failed', 'cancelled')

    @property
    def hold_expired(self):
        """An unpaid booking whose slot hold has lapsed — an abandoned
        checkout. The SAME rule _active_bookings uses to stop the hold
        blocking the slot (bookings/holds.py), so the panel calls a hold
        abandoned exactly when availability frees it."""
        from . import holds

        if self.status != 'payment_pending' or self.created_at is None:
            return False
        return not holds.is_live(self)

    @property
    def hold_expiry(self):
        from . import holds
        return holds.expires_at(self)

    @property
    def hold_state(self):
        """'held' | 'payment_pending' | 'expired' for an unpaid row, else
        None (bookings/holds.py)."""
        from . import holds
        return holds.hold_state(self)

    @property
    def display_status(self):
        """Stored status, except that 'confirmed' becomes 'completed' once the
        last slot has ended (IST) — the same rule the admin panel uses."""
        from .slots import now_minutes_ist, slots_end_minute, today_ist

        if self.status and self.status != 'confirmed':
            return self.status
        today = today_ist()
        if self.kind == 'membership':
            # A membership is "completed" when its validity ends, not on the
            # day it started (its `date`).
            membership = getattr(self, 'membership', None)
            ends = membership.ends_on if membership else self.date
            return 'completed' if ends < today else 'confirmed'
        if self.date < today:
            return 'completed'
        if self.date == today:
            end = slots_end_minute(self.slots)
            if end and end <= now_minutes_ist():
                return 'completed'
        return 'confirmed'

    @property
    def user_rating(self):
        """The customer's own stars for this booking, or None. Django makes a
        missing reverse one-to-one raise AttributeError, so getattr's default
        covers it. Callers listing many bookings should
        select_related('rating') to avoid a query per row."""
        rating = getattr(self, 'rating', None)
        return rating.stars if rating else None

    @property
    def user_review(self):
        """The customer's own written review on this booking ('' or None)."""
        rating = getattr(self, 'rating', None)
        return (rating.review or None) if rating else None

    @property
    def rating_status(self):
        """See bookings.ratings.booking_rating_status."""
        from .ratings import booking_rating_status
        return booking_rating_status(self)

    def rating_fields(self):
        """The rating keys the CUSTOMER's booking history adds to as_record:
        the server's answer to "may I rate this now?" — 'eligible' | 'rated'
        | 'not_completed' | 'not_eligible' — plus the review already given.
        The customer app prompts on 'eligible' only, never on its own clock.
        Kept out of as_record so vendor/admin lists don't pay for it."""
        return {'ratingStatus': self.rating_status, 'userReview': self.user_review}

    @property
    def online_amount(self):
        """What the gateway actually charged. Pre-part-payment bookings have
        no pay_now, and for those the online charge WAS the full amount."""
        return self.amount if self.pay_now is None else self.pay_now

    @property
    def venue_due(self):
        """Cash the vendor collects on arrival — zero unless part-paid."""
        return self.at_venue or 0

    def as_record(self):
        """The camelCase booking record shape the frontend expects."""
        return {
            'id': self.id,
            'status': self.display_status,
            'phone': self.phone or None,
            'customer': self.customer_name,
            'venueName': self.venue_name,
            # The listing booked (a unit's own listing for pitch/screen
            # bookings). Lets the vendor dashboard filter by venue on the id
            # rather than the name frozen at booking time.
            'listingId': str(self.listing_id) if self.listing_id else None,
            'category': self.category,
            'location': self.location,
            'image': self.image,
            'date': self.date.isoformat(),
            'slots': self.slots,
            'sport': self.sport or None,
            'unit': self.unit,
            'unitLabel': self.unit_label or None,
            'occasion': self.occasion or None,
            'occasionNote': self.occasion_note or None,
            'payNow': self.online_amount,
            'atVenue': self.venue_due,
            'partPayment': self.part_payment or None,
            'perSlot': self.per_slot,
            'addons': self.addons,
            'offer': self.offer or None,
            'discountAmount': self.discount_amount,
            'fee': self.fee,
            'amount': self.amount,
            'method': self.method,
            'walkIn': self.walk_in,
            'userRating': self.user_rating,
            'collected': self.collected,
            'collectedAt': self.collected_at.isoformat() if self.collected_at else None,
            'checkedInAt': self.checked_in_at.isoformat() if self.checked_in_at else None,
            'createdAt': self.created_at.isoformat(),
            # Booking Info only (the apps keep them off list rows). as_record
            # is served solely to the booking's own customer, its venue's
            # vendor and the admin — never on a public endpoint.
            'invoiceNumber': self.invoice_number or None,
            **payment_time_fields(self),
            # Booking status and payment status as two separate answers
            # (bookings/status_view.py) — `status` above stays as it was.
            **self.state_fields(),
            # Unpaid rows only: 'held' (reserved at checkout), 'payment_pending'
            # (payment started) or 'expired' — and when the 5-minute hold
            # ends (bookings/holds.py). Null on every other row.
            'holdState': self.hold_state,
            'holdExpiresAt': (
                self.hold_expiry.isoformat()
                if self.status == 'payment_pending' and self.hold_expiry else None
            ),
            # Additive: 'slot' for ordinary bookings, else what was bought,
            # with its details (offerings/summary.py; None for slots).
            'kind': self.kind or 'slot',
            'offering': self.offering_summary(),
        }

    def offering_summary(self):
        if not self.kind:
            return None
        from offerings.summary import offering_summary
        return offering_summary(self)

    def state_fields(self):
        from .status_view import booking_states, state_fields
        return state_fields(booking_states(self))

    def __init__(self, *args, **kwargs):
        # Rows loaded from the database arrive positionally (Model.from_db);
        # only a brand-new booking built without an explicit id gets a
        # GENERATED one — the only kind save() may swap on a clash.
        generated = not args and 'id' not in kwargs and 'pk' not in kwargs
        super().__init__(*args, **kwargs)
        self._generated_id = generated

    def save(self, *args, **kwargs):
        """A new booking is inserted under a platform-unique 6-character ID
        (unique across venue bookings AND event orders, enforced by
        BookingCode) and given its invoice number in the same transaction
        (bookings/invoices.py). Updates save exactly as before — an existing
        invoice number is never replaced."""
        if not self._state.adding:
            return super().save(*args, **kwargs)
        return insert_with_invoice(self, lambda: insert_with_code(
            self, 'venue', lambda: super(Booking, self).save(*args, **kwargs)
        ))

    def __str__(self):
        return f'{self.id} — {self.venue_name} on {self.date}'


class BookingCode(models.Model):
    """Every 6-character booking ID ever issued, across ALL booking types.

    Venue bookings and event ticket orders live in separate tables, so their
    own primary keys cannot stop the two from issuing the same ID. This one
    table can: `code` is its primary key, and each new booking / order
    inserts its code here in the same savepoint as itself
    (booking_codes.insert_with_code). A clash fails the insert and a fresh
    code is drawn. Nothing reads it for display — it is purely the
    uniqueness guarantee.
    """

    code = models.CharField(max_length=6, primary_key=True)
    kind = models.CharField(max_length=10)   # 'venue' | 'event'
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.code} ({self.kind})'


class InvoiceSequence(models.Model):
    """The invoice-number counter: one row per financial year ('2627' =
    1 Apr 2026 – 31 Mar 2027 IST), holding the last serial issued.

    Venue bookings and event orders share it, so the platform has ONE
    invoice series. bookings/invoices.py increments it with a conditional
    UPDATE in the same transaction as the booking insert — the row lock that
    UPDATE takes is what stops two simultaneous bookings from getting the
    same number. Nothing else writes it.
    """

    financial_year = models.CharField(max_length=4, primary_key=True)
    last_number = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f'FY {self.financial_year}: {self.last_number}'


class Rating(models.Model):
    """One customer rating for one completed booking (1–5 stars).

    The OneToOne on booking enforces "one rating per booking" at the DB level
    — a double-submit can never skew the average.

    The SAME table rates event tickets: a row is either a venue rating
    (booking + listing) or an event rating (event_order + event), never both
    — the check constraint below says so. Every rating written before events
    could be rated is a venue rating, so existing rows satisfy it unchanged.
    One table means one set of rules (bookings/ratings.py) for both kinds.
    """

    booking = models.OneToOneField(
        Booking, null=True, blank=True, on_delete=models.CASCADE, related_name='rating'
    )
    listing = models.ForeignKey(
        'venues.Listing', null=True, blank=True,
        on_delete=models.CASCADE, related_name='ratings',
    )
    # Event tickets. SET_NULL on the event, like EventOrder.event: a genuine
    # review must not vanish if an event row is ever hard-deleted.
    event_order = models.OneToOneField(
        'events.EventOrder', null=True, blank=True,
        on_delete=models.CASCADE, related_name='rating',
    )
    event = models.ForeignKey(
        'events.Event', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ratings',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ratings',
    )
    stars = models.PositiveSmallIntegerField()
    # Optional written review. Blank for every rating made before reviews
    # existed (stars only) and whenever the customer leaves it empty.
    review = models.TextField(blank=True, default='')
    # Super-admin moderation: a hidden rating stays in the table (it is a
    # real customer's submission) but leaves every average, count and public
    # review list. Set and cleared only by AdminRatingDetailView.
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_reason = models.CharField(max_length=200, blank=True, default='')
    # The venue / event owner's public reply (bookings/ratings.py). The
    # vendor can write, edit or remove THEIR reply — never the customer's
    # stars or words. A super admin can hide a reply (reply_hidden_at) the
    # same way they hide a review; a hidden reply stays hidden until the
    # admin restores it.
    reply = models.TextField(blank=True, default='')
    replied_at = models.DateTimeField(null=True, blank=True)
    reply_hidden_at = models.DateTimeField(null=True, blank=True)
    reply_hidden_reason = models.CharField(max_length=200, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # The longest written review accepted (characters).
    MAX_REVIEW_LENGTH = 1000
    MAX_REPLY_LENGTH = 600

    @property
    def public_reply(self):
        """{text, repliedAt} for public lists, or None (none / hidden)."""
        if not self.reply or self.reply_hidden_at is not None:
            return None
        return {
            'text': self.reply,
            'repliedAt': self.replied_at.isoformat() if self.replied_at else None,
        }

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(booking__isnull=False, event_order__isnull=True)
                    | models.Q(booking__isnull=True, event_order__isnull=False)
                ),
                name='rating_is_for_a_booking_or_a_ticket',
            ),
        ]

    def __str__(self):
        if self.event_order_id:
            return f'{self.stars}★ for event {self.event_id} ({self.event_order_id})'
        return f'{self.stars}★ for {self.listing_id} ({self.booking_id})'
