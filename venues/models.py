import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from accounts.models import phone_validator

# --- Reusable validators -----------------------------------------

pincode_validator = RegexValidator(
    regex=r'^\d{6}$',
    message='Pincode must be exactly 6 digits.',
)
account_number_validator = RegexValidator(
    regex=r'^\d{9,18}$',
    message='Account number must be 9 to 18 digits.',
)
ifsc_validator = RegexValidator(
    regex=r'^[A-Z]{4}0[A-Z0-9]{6}$',
    message='Enter a valid IFSC code, e.g. HDFC0001234.',
)
pan_validator = RegexValidator(
    regex=r'^[A-Z]{5}[0-9]{4}[A-Z]$',
    message='Enter a valid PAN, e.g. ABCDE1234F.',
)


class Venue(models.Model):
    """
    One venue listing, created step-by-step through the vendor wizard.
    Starts as an empty DRAFT, so almost every field allows blank —
    completeness is checked only at submit time.
    """

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'          # vendor still editing
        PENDING = 'PENDING', 'Pending'    # submitted, waiting for admin review
        LIVE = 'LIVE', 'Live'             # approved, visible to the public
        REJECTED = 'REJECTED', 'Rejected' # admin rejected (see rejection_reason)

    class Category(models.TextChoices):
        PRIVATE_HALL = 'PRIVATE_HALL', 'Private Hall'
        PRIVATE_THEATRE = 'PRIVATE_THEATRE', 'Private Theatre'
        OPEN_THEATRE = 'OPEN_THEATRE', 'Open Theatre'
        RESORT = 'RESORT', 'Resort'
        PLAYZONE = 'PLAYZONE', 'Playzone'

    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='venues',
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    rejection_reason = models.TextField(blank=True, default='')
    # Soft delete: we never really erase a venue (bookings may reference it);
    # we just hide it everywhere by filtering is_deleted=False.
    is_deleted = models.BooleanField(default=False)

    # --- Basic info ---
    name = models.CharField(max_length=200, blank=True, default='')
    short_description = models.CharField(max_length=300, blank=True, default='')
    contact_phone = models.CharField(
        max_length=10, blank=True, default='', validators=[phone_validator]
    )
    contact_email = models.EmailField(blank=True, default='')
    partner_email = models.EmailField(blank=True, default='')

    # --- Location ---
    address_line = models.CharField(max_length=300, blank=True, default='')
    locality = models.CharField(max_length=100, blank=True, default='')
    district = models.CharField(max_length=100, blank=True, default='')
    pincode = models.CharField(
        max_length=6, blank=True, default='', validators=[pincode_validator]
    )
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=100, blank=True, default='')
    maps_link = models.URLField(max_length=500, blank=True, default='')

    # --- Category & features ---
    # null until the vendor picks one in the wizard
    category = models.CharField(
        max_length=20, choices=Category.choices, null=True, blank=True
    )
    parking = models.BooleanField(default=False)
    dining = models.BooleanField(default=False)
    # JSON lists, e.g. ["WiFi", "AC"] — flexible without extra tables.
    amenities = models.JSONField(default=list, blank=True)
    # Only meaningful for PRIVATE_HALL (e.g. ["Wedding", "Birthday"]).
    occasions = models.JSONField(default=list, blank=True)
    full_description = models.TextField(blank=True, default='')

    # --- Category-specific pricing fields ---
    # PLAYZONE only: total people the site can hold.
    site_capacity = models.PositiveIntegerField(null=True, blank=True)
    # Charge for extra guests beyond a package limit (NOT allowed for RESORT).
    extra_person_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    extra_person_max = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            # Fast lookups for "my venues" (every vendor request filters by these).
            models.Index(fields=['vendor']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f'{self.name or "(unnamed venue)"} [{self.status}]'


class Unit(models.Model):
    """
    A bookable space inside a venue. The meaning depends on category:
    hall ("Hall 1"), screen ("Screen 1"), lawn ("Lawn 1"), etc.
    """

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='units')
    label = models.CharField(max_length=100)
    # Nullable while drafting; submit requires every unit to have it.
    max_persons = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f'{self.label} ({self.venue_id})'


class Package(models.Model):
    """A priced offering, e.g. 'Birthday Package — 3 hrs — Rs 4999'."""

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='packages')
    label = models.CharField(max_length=150)
    details = models.TextField(blank=True, default='')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    duration_hrs = models.PositiveIntegerField(null=True, blank=True)
    max_persons = models.PositiveIntegerField(null=True, blank=True)
    # True = price is per hour; False = flat price for the package.
    charge_per_hour = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.label} — {self.price}'


class SportPricing(models.Model):
    """PLAYZONE only: hourly price for one sport at this venue."""

    class Sport(models.TextChoices):
        BOX_CRICKET = 'BOX_CRICKET', 'Box Cricket'
        BADMINTON = 'BADMINTON', 'Badminton'
        VOLLEYBALL = 'VOLLEYBALL', 'Volleyball'
        BASKETBALL = 'BASKETBALL', 'Basketball'
        SWIMMING_POOL = 'SWIMMING_POOL', 'Swimming Pool'
        PICKLEBALL = 'PICKLEBALL', 'Pickleball'
        FOOTBALL = 'FOOTBALL', 'Football'

    class CapacityType(models.TextChoices):
        UNLIMITED = 'UNLIMITED', 'Unlimited'
        LIMITED = 'LIMITED', 'Limited'

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='sports')
    sport = models.CharField(max_length=20, choices=Sport.choices)
    price_per_hour = models.DecimalField(max_digits=10, decimal_places=2)
    capacity_type = models.CharField(
        max_length=10, choices=CapacityType.choices, default=CapacityType.UNLIMITED
    )
    # Required when capacity_type=LIMITED (enforced in the serializer).
    max_persons = models.PositiveIntegerField(null=True, blank=True)
    # How many courts/pitches of this sport exist.
    pitches = models.PositiveIntegerField(default=1)

    class Meta:
        # A venue can price each sport only once.
        constraints = [
            models.UniqueConstraint(fields=['venue', 'sport'], name='unique_venue_sport'),
        ]

    def __str__(self):
        return f'{self.sport} @ {self.venue_id}'


class Addon(models.Model):
    """Optional extra a customer can add, e.g. 'Photographer — Rs 2000'."""

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='addons')
    name = models.CharField(max_length=150)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # True = customer picks a quantity (e.g. 3 x cake); False = on/off.
    quantity_based = models.BooleanField(default=False)

    def __str__(self):
        return f'{self.name} — {self.price}'


class VenuePhoto(models.Model):
    """
    A photo of the venue. The image FILE lives on Cloudinary
    (uploaded by the frontend) — we store only its URL.
    """

    MAX_PER_TYPE = 5

    class Type(models.TextChoices):
        VENUE = 'VENUE', 'Venue'      # the place itself
        SERVICE = 'SERVICE', 'Service'  # food, decoration, activities...

    venue = models.ForeignKey(Venue, on_delete=models.CASCADE, related_name='photos')
    image_url = models.URLField(max_length=500)
    type = models.CharField(max_length=10, choices=Type.choices, default=Type.VENUE)
    order = models.PositiveIntegerField(default=0)  # display order in the gallery

    class Meta:
        ordering = ['order', 'id']

    def clean(self):
        """Enforce the 'max 5 photos per type' rule at the model level."""
        existing = VenuePhoto.objects.filter(venue=self.venue, type=self.type)
        if self.pk:
            existing = existing.exclude(pk=self.pk)  # editing shouldn't count itself
        if existing.count() >= self.MAX_PER_TYPE:
            raise ValidationError(
                f'Maximum {self.MAX_PER_TYPE} photos allowed for type {self.type}.'
            )

    def __str__(self):
        return f'{self.type} photo #{self.order} of venue {self.venue_id}'


class PayoutDetails(models.Model):
    """
    Bank details for paying the vendor — one per user (OneToOne).
    Sensitive: API responses must MASK account_number (only last 4 digits).
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payout_details',
    )
    account_holder = models.CharField(max_length=150)
    bank_name = models.CharField(max_length=150)
    account_number = models.CharField(
        max_length=18, validators=[account_number_validator]
    )
    ifsc = models.CharField(max_length=11, validators=[ifsc_validator])
    payout_phone = models.CharField(max_length=10, validators=[phone_validator])
    # Optional fields — validators are skipped automatically when blank.
    upi_id = models.CharField(max_length=100, blank=True, default='')
    pan = models.CharField(max_length=10, blank=True, default='', validators=[pan_validator])

    class Meta:
        verbose_name_plural = 'Payout details'

    def __str__(self):
        return f'Payout details of {self.user}'


def empty_draft_data():
    """The 5 buckets every draft starts with (frontend contract shape)."""
    return {
        'basics': {},
        'location': {},
        'details': {},
        'payout': {},
        'photos': {'venuePhotos': [], 'serviceImages': []},
    }


class VenueDraft(models.Model):
    """
    A venue-registration draft (frontend wizard, Group 4 of the contract).

    The wizard autosaves free-form JSON "buckets" (basics/location/details/
    payout/photos). We store them EXACTLY as the frontend sends them, so
    what a vendor saved is byte-for-byte what they get back on reload.
    On submit, the known fields are mapped into the structured Venue model.

    The UUID primary key doubles as the public listing id later —
    the contract requires "resubmit updates, never duplicates".
    """

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        PENDING = 'pending', 'Pending'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='drafts',
    )
    data = models.JSONField(default=empty_draft_data)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'Draft {self.id} ({self.vendor})'


class Listing(models.Model):
    """
    A published venue listing (what the public browses).

    Shares its UUID with the draft it was published from — that is how
    "resubmit updates, never duplicates" works (contract 3.5).

    The full record the frontend sent (incl. gallery + detail block) is
    stored as JSON and served back verbatim; a few fields are ALSO
    extracted into real indexed columns so search/filter stays fast.
    """

    class Status(models.TextChoices):
        LIVE = 'live', 'Live'          # visible to the public (auto-approve for now)
        PENDING = 'pending', 'Pending' # future: waiting for admin review

    id = models.UUIDField(primary_key=True, editable=False)
    vendor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='listings',
    )
    slug = models.SlugField(max_length=220, unique=True)
    record = models.JSONField(default=dict)

    # Extracted from record purely for fast filtering/search:
    name = models.CharField(max_length=200, blank=True, default='')
    category = models.CharField(max_length=50, blank=True, default='')
    locality = models.CharField(max_length=120, blank=True, default='')
    pincode = models.CharField(max_length=10, blank=True, default='')

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.LIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['category']),
            models.Index(fields=['locality']),
            models.Index(fields=['pincode']),
        ]

    def __str__(self):
        return f'{self.name or self.slug} [{self.status}]'
