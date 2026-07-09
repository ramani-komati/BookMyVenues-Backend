from django.db import transaction
from rest_framework import serializers

from .completion import compute_completion
from .models import Addon, Package, PayoutDetails, SportPricing, Unit, Venue, VenuePhoto


class UnitSerializer(serializers.ModelSerializer):
    class Meta:
        model = Unit
        fields = ['id', 'label', 'max_persons']


class PackageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Package
        fields = ['id', 'label', 'details', 'price', 'duration_hrs', 'max_persons', 'charge_per_hour']


class SportPricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SportPricing
        fields = ['id', 'sport', 'price_per_hour', 'capacity_type', 'max_persons', 'pitches']

    def validate(self, attrs):
        # Business rule: LIMITED capacity makes no sense without a limit.
        if attrs.get('capacity_type') == SportPricing.CapacityType.LIMITED and not attrs.get('max_persons'):
            raise serializers.ValidationError(
                {'max_persons': 'max_persons is required when capacity_type is LIMITED.'}
            )
        return attrs


class AddonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Addon
        fields = ['id', 'name', 'price', 'quantity_based']


class VenuePhotoSerializer(serializers.ModelSerializer):
    class Meta:
        model = VenuePhoto
        fields = ['id', 'image_url', 'type', 'order']


class VenuePhotoCreateSerializer(serializers.ModelSerializer):
    """Input for POST .../photos — just the URL and the gallery type."""

    class Meta:
        model = VenuePhoto
        fields = ['image_url', 'type']


class PayoutDetailsSerializer(serializers.ModelSerializer):
    """
    Payout bank details. Validators (account number 9-18 digits, IFSC,
    phone, optional UPI/PAN) come from the model fields automatically.
    """

    class Meta:
        model = PayoutDetails
        fields = [
            'account_holder', 'bank_name', 'account_number',
            'ifsc', 'payout_phone', 'upi_id', 'pan',
        ]

    def to_representation(self, instance):
        """SECURITY: mask the account number in every response —
        the full number never leaves the server (e.g. **********7842)."""
        data = super().to_representation(instance)
        account_number = data.get('account_number') or ''
        if account_number:
            data['account_number'] = '*' * (len(account_number) - 4) + account_number[-4:]
        return data


class VenueListSerializer(serializers.ModelSerializer):
    """Compact row for GET /vendor/venues — matches the contract."""

    completion = serializers.SerializerMethodField()

    class Meta:
        model = Venue
        fields = ['id', 'name', 'category', 'status', 'completion']

    def get_completion(self, venue):
        percent, _ = compute_completion(venue)
        return percent


class VenueDetailSerializer(serializers.ModelSerializer):
    """Full object for GET /vendor/venues/<id> — everything the wizard needs."""

    units = UnitSerializer(many=True, read_only=True)
    packages = PackageSerializer(many=True, read_only=True)
    sports = SportPricingSerializer(many=True, read_only=True)
    addons = AddonSerializer(many=True, read_only=True)
    photos = VenuePhotoSerializer(many=True, read_only=True)
    completion = serializers.SerializerMethodField()
    missing = serializers.SerializerMethodField()

    class Meta:
        model = Venue
        fields = [
            'id', 'status', 'rejection_reason',
            # basic info
            'name', 'short_description', 'contact_phone', 'contact_email', 'partner_email',
            # location
            'address_line', 'locality', 'district', 'pincode', 'city', 'state', 'maps_link',
            # category & features
            'category', 'parking', 'dining', 'amenities', 'occasions', 'full_description',
            # category-specific pricing
            'site_capacity', 'extra_person_price', 'extra_person_max',
            # related lists
            'units', 'packages', 'sports', 'addons', 'photos',
            # progress
            'completion', 'missing',
            'created_at', 'updated_at',
        ]

    def get_completion(self, venue):
        # Cache so completion+missing are computed once per request, not twice.
        if not hasattr(self, '_completion_cache'):
            self._completion_cache = compute_completion(venue)
        return self._completion_cache[0]

    def get_missing(self, venue):
        if not hasattr(self, '_completion_cache'):
            self._completion_cache = compute_completion(venue)
        return self._completion_cache[1]


class VenueUpdateSerializer(serializers.ModelSerializer):
    """
    PATCH /vendor/venues/<id> — accepts any subset of fields.

    Nested lists (units, packages, sports, addons) REPLACE all existing
    rows of that kind when provided; omitted lists are left untouched.
    """

    # required=False: PATCH may send any subset.
    units = UnitSerializer(many=True, required=False)
    packages = PackageSerializer(many=True, required=False)
    sports = SportPricingSerializer(many=True, required=False)
    addons = AddonSerializer(many=True, required=False)
    # Ensure amenities/occasions are lists of strings, not arbitrary JSON.
    amenities = serializers.ListField(child=serializers.CharField(), required=False)
    occasions = serializers.ListField(child=serializers.CharField(), required=False)

    class Meta:
        model = Venue
        fields = [
            'name', 'short_description', 'contact_phone', 'contact_email', 'partner_email',
            'address_line', 'locality', 'district', 'pincode', 'city', 'state', 'maps_link',
            'category', 'parking', 'dining', 'amenities', 'occasions', 'full_description',
            'site_capacity', 'extra_person_price', 'extra_person_max',
            'units', 'packages', 'sports', 'addons',
        ]

    def validate(self, attrs):
        """
        Category-aware rules. The category may arrive in this very PATCH
        or already be saved on the venue — check against whichever applies.
        """
        category = attrs.get('category', self.instance.category if self.instance else None)
        errors = {}

        if attrs.get('occasions') and category != Venue.Category.PRIVATE_HALL:
            errors['occasions'] = 'Occasions are only allowed for PRIVATE_HALL venues.'

        if attrs.get('sports') and category != Venue.Category.PLAYZONE:
            errors['sports'] = 'Sports pricing is only allowed for PLAYZONE venues.'

        if attrs.get('site_capacity') is not None and category != Venue.Category.PLAYZONE:
            errors['site_capacity'] = 'Site capacity is only allowed for PLAYZONE venues.'

        if category == Venue.Category.RESORT:
            if attrs.get('extra_person_price') is not None:
                errors['extra_person_price'] = 'Extra person pricing is not allowed for RESORT venues.'
            if attrs.get('extra_person_max') is not None:
                errors['extra_person_max'] = 'Extra person max is not allowed for RESORT venues.'

        # The DB blocks duplicate sports; catch it here for a friendly error.
        sports = attrs.get('sports') or []
        sport_names = [s['sport'] for s in sports]
        if len(sport_names) != len(set(sport_names)):
            errors['sports'] = 'Each sport can only be priced once.'

        if errors:
            raise serializers.ValidationError(errors)
        return attrs

    def update(self, instance, validated_data):
        # Pull nested lists out — super().update() handles only scalar fields.
        # "None" means "not sent" (leave alone); "[]" means "delete all".
        units = validated_data.pop('units', None)
        packages = validated_data.pop('packages', None)
        sports = validated_data.pop('sports', None)
        addons = validated_data.pop('addons', None)

        # transaction.atomic = all-or-nothing: if anything fails midway,
        # the database rolls back and no half-updated venue is saved.
        with transaction.atomic():
            instance = super().update(instance, validated_data)

            # If the category changed, silently clear stored data that is
            # invalid for the NEW category (e.g. occasions on a RESORT).
            changed = False
            if instance.category != Venue.Category.PRIVATE_HALL and instance.occasions:
                instance.occasions = []
                changed = True
            if instance.category != Venue.Category.PLAYZONE:
                if instance.site_capacity is not None:
                    instance.site_capacity = None
                    changed = True
                if sports is None and instance.sports.exists():
                    instance.sports.all().delete()
            if instance.category == Venue.Category.RESORT and (
                instance.extra_person_price is not None or instance.extra_person_max is not None
            ):
                instance.extra_person_price = None
                instance.extra_person_max = None
                changed = True
            if changed:
                instance.save()

            # REPLACE semantics for each nested list that was provided.
            replacements = [
                (units, instance.units, Unit),
                (packages, instance.packages, Package),
                (sports, instance.sports, SportPricing),
                (addons, instance.addons, Addon),
            ]
            for data, manager, model_class in replacements:
                if data is not None:
                    manager.all().delete()
                    model_class.objects.bulk_create(
                        model_class(venue=instance, **row) for row in data
                    )

        return instance
