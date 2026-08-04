"""
Booking model — one confirmed reservation of time slots at a venue.
"""
import uuid

from django.conf import settings
from django.db import models


def make_booking_id():
    """Contract-style ids, e.g. 'bk_3f8a92c1d04e'."""
    return 'bk_' + uuid.uuid4().hex[:12]


class Booking(models.Model):
    class Method(models.TextChoices):
        ONLINE = 'online', 'Online'          # generic online payment
        UPI = 'upi', 'UPI'
        CARD = 'card', 'Card'
        NETBANKING = 'netbanking', 'Netbanking'
        VENUE = 'venue', 'Pay at venue'      # customer pays FULL amount on arrival
        WALK_IN = 'walk-in', 'Walk-in'       # vendor-recorded offline booking

    # Methods a customer may send on POST /users/me/bookings. Everything except
    # 'venue' means the money was collected online (payout passes it through).
    CUSTOMER_METHODS = {'online', 'upi', 'card', 'netbanking', 'venue'}

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
    # Lifecycle state. 'payment_pending' = created for a Razorpay order,
    # holds the slot for PENDING_HOLD_MINUTES until paid; 'completed' is
    # derived from the date; an admin can set 'refunded' etc.
    status = models.CharField(max_length=20, default='confirmed')
    # Razorpay linkage (empty for pay-at-venue / walk-ins / legacy bookings).
    razorpay_order_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True, default='')
    refund_id = models.CharField(max_length=64, blank=True, default='')
    # Set by the admin Refunds panel when issuing a refund.
    refund_reason = models.CharField(max_length=200, blank=True, default='')
    refund_amount = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Overlap checks always query one venue on one date.
            models.Index(fields=['listing', 'date']),
            models.Index(fields=['user', 'date']),
        ]

    def as_record(self):
        """The camelCase booking record shape the frontend expects."""
        return {
            'id': self.id,
            'phone': self.phone or None,
            'customer': self.customer_name,
            'venueName': self.venue_name,
            'category': self.category,
            'location': self.location,
            'image': self.image,
            'date': self.date.isoformat(),
            'slots': self.slots,
            'sport': self.sport or None,
            'unit': self.unit,
            'unitLabel': self.unit_label or None,
            'perSlot': self.per_slot,
            'addons': self.addons,
            'offer': self.offer or None,
            'discountAmount': self.discount_amount,
            'fee': self.fee,
            'amount': self.amount,
            'method': self.method,
            'walkIn': self.walk_in,
            'createdAt': self.created_at.isoformat(),
        }

    def __str__(self):
        return f'{self.id} — {self.venue_name} on {self.date}'


class Rating(models.Model):
    """One customer rating for one completed booking (1–5 stars).

    The OneToOne on booking enforces "one rating per booking" at the DB level
    — a double-submit can never skew the average."""

    booking = models.OneToOneField(
        Booking, on_delete=models.CASCADE, related_name='rating'
    )
    listing = models.ForeignKey(
        'venues.Listing', on_delete=models.CASCADE, related_name='ratings'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='ratings',
    )
    stars = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.stars}★ for {self.listing_id} ({self.booking_id})'
