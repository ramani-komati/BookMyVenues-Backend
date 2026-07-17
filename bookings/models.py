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
        ONLINE = 'online', 'Online'
        WALK_IN = 'walk-in', 'Walk-in'

    id = models.CharField(
        max_length=20, primary_key=True, default=make_booking_id, editable=False
    )
    listing = models.ForeignKey(
        'venues.Listing', on_delete=models.CASCADE, related_name='bookings'
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

    date = models.DateField()
    slots = models.JSONField(default=list)      # ["19:30 – 21:00", ...]
    per_slot = models.PositiveIntegerField(default=0)  # hourly rate (₹)
    addons = models.JSONField(default=list)     # [{"name", "qty", "price"}]
    amount = models.PositiveIntegerField()      # total, server-computed (₹)

    method = models.CharField(max_length=10, choices=Method.choices, default=Method.ONLINE)
    walk_in = models.BooleanField(default=False)
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
            'perSlot': self.per_slot,
            'addons': self.addons,
            'amount': self.amount,
            'method': self.method,
            'walkIn': self.walk_in,
            'createdAt': self.created_at.isoformat(),
        }

    def __str__(self):
        return f'{self.id} — {self.venue_name} on {self.date}'
