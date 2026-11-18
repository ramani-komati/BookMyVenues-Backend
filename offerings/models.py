"""
What Fitness, Resort and Big Screen Surprise venues SELL, and what customers
bought.

WHERE THE CONFIGURATION COMES FROM. Vendors (and the super admin, through the
same endpoints) fill the registration wizard; the wizard publishes the venue
record with a `detail.fitness` / `detail.resort` / `detail.surprise` block;
venues/views.py validates that block (offerings/schema.py) and syncs it into
the tables below (offerings/sync.py). These rows — not the public record —
are what checkout prices and books against.

Rows are keyed by the id the form gave each plan / package / show, so a
republish UPDATES them in place. One the vendor removes is ARCHIVED, never
deleted: customer memberships and bookings point at it (PROTECT), and their
history must keep its name and price.

WHAT CUSTOMERS BUY. Each purchase is an ordinary bookings.Booking — so the
existing Razorpay order → verify → webhook flow, invoice numbers, booking IDs,
refunds, payouts and every admin / vendor list cover it unchanged — with
`Booking.kind` saying what it is, plus one row here holding what only that
kind has: a membership's validity and visits, a package's guests and slot, a
surprise's show and personal message.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q


class OfferingProfile(models.Model):
    """The venue-wide settings checkout needs: open days, closures, the
    surprise price / occasions / upload rules … (the normalised record block,
    minus its lists of plans / packages / shows, which have their own rows)."""

    class Kind(models.TextChoices):
        FITNESS = 'fitness', 'Fitness'
        RESORT = 'resort', 'Resort'
        SURPRISE = 'surprise', 'Big Screen Surprise'

    listing = models.OneToOneField(
        'venues.Listing', on_delete=models.CASCADE, related_name='offering_profile'
    )
    kind = models.CharField(max_length=10, choices=Kind.choices)
    settings = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.kind} profile of {self.listing_id}'


class _Archivable(models.Model):
    """Shared by the three configuration tables."""

    # The id the registration form gave the row — stable across republishes.
    key = models.CharField(max_length=40)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    # Set when the vendor removed it from the form. Kept for history.
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def on_sale(self):
        return self.enabled and self.archived_at is None


class FitnessPlan(_Archivable):
    """One membership plan a gym sells (day pass, monthly, PT package …)."""

    listing = models.ForeignKey(
        'venues.Listing', on_delete=models.CASCADE, related_name='fitness_plans'
    )
    kind = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True, default='')
    price = models.PositiveIntegerField()                    # ₹
    duration_value = models.PositiveSmallIntegerField()
    duration_unit = models.CharField(max_length=5)           # day | week | month
    start_rule = models.CharField(max_length=10, default='purchase')
    start_within_days = models.PositiveSmallIntegerField(default=0)
    expiry_rule = models.CharField(max_length=20, default='duration')
    # Visits / sessions / classes the plan allows; null = unlimited.
    visits = models.PositiveIntegerField(null=True, blank=True)
    facilities = models.JSONField(default=list, blank=True)
    trial_available = models.BooleanField(default=False)
    trial_price = models.PositiveIntegerField(null=True, blank=True)
    # Charged on a customer's FIRST membership at this gym only.
    joining_fee = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['listing', 'key'], name='unique_fitness_plan_key'),
            models.CheckConstraint(condition=Q(duration_value__gte=1), name='fitness_plan_duration_positive'),
        ]

    def __str__(self):
        return f'{self.name} — ₹{self.price}'


class ResortPackage(_Archivable):
    """A resort day package (Half / Full Day) or a pool / water-park entry."""

    listing = models.ForeignKey(
        'venues.Listing', on_delete=models.CASCADE, related_name='resort_packages'
    )
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=500, blank=True, default='')
    kind = models.CharField(max_length=5)                    # day | pool
    duration = models.CharField(max_length=10, blank=True, default='')  # half_day | full_day
    pricing = models.CharField(max_length=12)                # per_person | per_package | per_hour
    price = models.PositiveIntegerField()                    # adult / package price, ₹
    child_price = models.PositiveIntegerField(null=True, blank=True)
    min_persons = models.PositiveIntegerField(default=1)
    max_persons = models.PositiveIntegerField()
    # Guests the resort can take for this package per date + slot, across all
    # bookings. Null = no limit beyond max_persons per booking.
    capacity = models.PositiveIntegerField(null=True, blank=True)
    # [{"start": "09:00", "end": "13:00"}] — half-day / pool slots. A full-day
    # package has none (it runs check_in → check_out).
    slots = models.JSONField(default=list, blank=True)
    check_in = models.CharField(max_length=5, blank=True, default='')
    check_out = models.CharField(max_length=5, blank=True, default='')
    available_from = models.DateField(null=True, blank=True)
    available_to = models.DateField(null=True, blank=True)
    inclusions = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['listing', 'key'], name='unique_resort_package_key'),
            models.CheckConstraint(
                condition=Q(min_persons__gte=1) & Q(max_persons__gte=models.F('min_persons')),
                name='resort_package_persons_range',
            ),
        ]

    def __str__(self):
        return f'{self.name} — ₹{self.price}'


class SurpriseShow(_Archivable):
    """A movie a Big Screen Surprise theatre runs, on one screen, at the
    listed show times, between two dates."""

    listing = models.ForeignKey(
        'venues.Listing', on_delete=models.CASCADE, related_name='surprise_shows'
    )
    movie = models.CharField(max_length=150)
    language = models.CharField(max_length=40, blank=True, default='')
    screen = models.PositiveSmallIntegerField(default=1)
    times = models.JSONField(default=list)                   # ["10:30", "14:15"]
    available_from = models.DateField(null=True, blank=True)
    available_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ['sort_order', 'id']
        constraints = [
            models.UniqueConstraint(fields=['listing', 'key'], name='unique_surprise_show_key'),
            models.CheckConstraint(condition=Q(screen__gte=1), name='surprise_show_screen_positive'),
        ]

    def __str__(self):
        return f'{self.movie} (screen {self.screen})'


# --- What customers bought ---------------------------------------------------

class Membership(models.Model):
    """A customer's gym membership. Payment state is the booking's; validity
    and visits live here."""

    booking = models.OneToOneField(
        'bookings.Booking', on_delete=models.CASCADE, related_name='membership'
    )
    plan = models.ForeignKey(FitnessPlan, on_delete=models.PROTECT, related_name='memberships')
    listing = models.ForeignKey(
        'venues.Listing', null=True, on_delete=models.SET_NULL, related_name='memberships'
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name='memberships'
    )
    # The plan as it was sold — a later price or name change never rewrites it.
    plan_snapshot = models.JSONField(default=dict)
    starts_on = models.DateField()
    ends_on = models.DateField()
    visits_allowed = models.PositiveIntegerField(null=True, blank=True)
    visits_used = models.PositiveIntegerField(default=0)
    joining_fee = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user']), models.Index(fields=['listing'])]
        constraints = [
            models.CheckConstraint(condition=Q(ends_on__gte=models.F('starts_on')), name='membership_dates_ordered'),
        ]

    def __str__(self):
        return f'{self.plan_snapshot.get("name") or self.plan_id} for {self.user_id}'


class MembershipVisit(models.Model):
    """One visit the gym recorded against a membership."""

    membership = models.ForeignKey(Membership, on_delete=models.CASCADE, related_name='visits')
    visited_on = models.DateField()
    recorded_at = models.DateTimeField(auto_now_add=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+'
    )

    class Meta:
        ordering = ['-recorded_at']


class PackageBooking(models.Model):
    """The resort-package side of a booking."""

    booking = models.OneToOneField(
        'bookings.Booking', on_delete=models.CASCADE, related_name='package_booking'
    )
    package = models.ForeignKey(ResortPackage, on_delete=models.PROTECT, related_name='bookings')
    package_snapshot = models.JSONField(default=dict)
    adults = models.PositiveIntegerField(default=1)
    children = models.PositiveIntegerField(default=0)
    # The slot booked, in minutes from midnight (full day = check-in → out).
    start_minute = models.PositiveSmallIntegerField()
    end_minute = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(end_minute__gt=models.F('start_minute')), name='package_booking_slot_ordered'),
            models.CheckConstraint(condition=Q(adults__gte=1), name='package_booking_has_an_adult'),
        ]

    @property
    def persons(self):
        return self.adults + self.children


class SurpriseBooking(models.Model):
    """The personalised surprise played on a theatre's big screen."""

    booking = models.OneToOneField(
        'bookings.Booking', on_delete=models.CASCADE, related_name='surprise_booking'
    )
    show = models.ForeignKey(SurpriseShow, on_delete=models.PROTECT, related_name='bookings')
    movie = models.CharField(max_length=150)
    screen = models.PositiveSmallIntegerField()
    show_minute = models.PositiveSmallIntegerField()          # show start, minutes from midnight
    occasion = models.CharField(max_length=40)
    recipient_name = models.CharField(max_length=60)
    message = models.CharField(max_length=200)
    sender_name = models.CharField(max_length=60, blank=True, default='')
    photo_url = models.URLField(max_length=500, blank=True, default='')
    video_url = models.URLField(max_length=500, blank=True, default='')

    class Meta:
        indexes = [models.Index(fields=['screen', 'show_minute'])]
