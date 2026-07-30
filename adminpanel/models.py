"""
Models that exist only for the super-admin panel (Phase 3):
Settings (a single config row), Payout, Review, and the AuditEntry log.
"""
from django.db import models


def _default_categories():
    return ['Private Hall', 'Private Theatre', 'Open Theatre', 'Resort', 'Playzone']


class Settings(models.Model):
    """Platform config — one row (pk=1). `booking_fee` is the live ₹ fee."""

    booking_fee = models.PositiveIntegerField(default=20)
    fee_date = models.CharField(max_length=20, blank=True, default='')
    commission = models.CharField(max_length=10, default='10')
    categories = models.JSONField(default=_default_categories, blank=True)
    cities = models.JSONField(default=list, blank=True)
    amenities = models.JSONField(default=list, blank=True)
    banners = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name_plural = 'Settings'

    @classmethod
    def load(cls):
        """The singleton config row, created with defaults on first use."""
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def __str__(self):
        return f'Platform settings (fee ₹{self.booking_fee})'


class Payout(models.Model):
    """One vendor's payout for one completed week (Mon–Sun) of ONLINE
    bookings. Generated automatically — see adminpanel/payouts.py."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        FAILED = 'failed', 'Failed'
        COMPLETED = 'completed', 'Completed'

    vendor = models.CharField(max_length=150, default='')  # display name
    vendor_user = models.ForeignKey(
        'accounts.User', null=True, blank=True,
        on_delete=models.SET_NULL, related_name='payouts',
    )
    period = models.CharField(max_length=50, blank=True, default='')  # "14–20 Jul"
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    gross = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            # One payout per vendor per week — generation is idempotent.
            models.UniqueConstraint(
                fields=['vendor_user', 'period_start'],
                name='unique_vendor_week_payout',
            ),
        ]


class Review(models.Model):
    """A customer review flagged for moderation."""

    class Status(models.TextChoices):
        FLAGGED = 'flagged', 'Flagged'
        KEPT = 'kept', 'Kept'
        REMOVED = 'removed', 'Removed'

    venue = models.CharField(max_length=200, default='')
    reviewer = models.CharField(max_length=150, default='')
    rating = models.PositiveSmallIntegerField(default=0)
    text = models.TextField(blank=True, default='')
    reason = models.CharField(max_length=200, blank=True, default='')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.FLAGGED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class AuditEntry(models.Model):
    """Append-only log of admin actions (written by the server and/or panel)."""

    time = models.CharField(max_length=50, blank=True, default='')
    admin = models.CharField(max_length=150, blank=True, default='')
    action = models.CharField(max_length=150, blank=True, default='')
    target = models.CharField(max_length=200, blank=True, default='')   # entity NAME
    target_id = models.CharField(max_length=64, blank=True, default='')  # entity id
    change = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
