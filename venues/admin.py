from django.contrib import admin

from .models import Addon, Listing, Package, PayoutDetails, SportPricing, Unit, Venue, VenueDraft, VenuePhoto

# "Inlines" let you edit a venue's units/packages/etc. directly
# on the Venue page in the admin, instead of on separate pages.


class UnitInline(admin.TabularInline):
    model = Unit
    extra = 0


class PackageInline(admin.TabularInline):
    model = Package
    extra = 0


class SportPricingInline(admin.TabularInline):
    model = SportPricing
    extra = 0


class AddonInline(admin.TabularInline):
    model = Addon
    extra = 0


class VenuePhotoInline(admin.TabularInline):
    model = VenuePhoto
    extra = 0


@admin.register(Venue)
class VenueAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'vendor', 'category', 'status', 'is_deleted', 'updated_at')
    list_filter = ('status', 'category', 'is_deleted')
    search_fields = ('name', 'vendor__phone', 'city', 'pincode')
    inlines = [UnitInline, PackageInline, SportPricingInline, AddonInline, VenuePhotoInline]


@admin.register(PayoutDetails)
class PayoutDetailsAdmin(admin.ModelAdmin):
    """Bank details are shown MASKED, matching the super-admin panel
    (adminpanel/formatters._mask_account). Nobody browsing Django admin needs
    a full account number on screen, and this page has no OTP gate."""

    list_display = ('user', 'account_holder', 'bank_name', 'ifsc', 'account_masked')
    search_fields = ('user__phone', 'account_holder')

    @admin.display(description='Account number')
    def account_masked(self, obj):
        number = str(getattr(obj, 'account_number', '') or '')
        return f'****{number[-4:]}' if len(number) > 4 else '****'

    def get_readonly_fields(self, request, obj=None):
        # Never editable here — payout details are the vendor's own data.
        return [f.name for f in self.model._meta.fields]


# Simple registrations so each model is also browsable on its own.
admin.site.register(Unit)
admin.site.register(Package)
admin.site.register(SportPricing)
admin.site.register(Addon)
admin.site.register(VenuePhoto)


@admin.register(VenueDraft)
class VenueDraftAdmin(admin.ModelAdmin):
    list_display = ['id', 'vendor', 'status', 'updated_at']
    list_filter = ['status']
    readonly_fields = ['id', 'created_at', 'updated_at']


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ['name', 'vendor', 'category', 'locality', 'status', 'updated_at']
    list_filter = ['status', 'category']
    search_fields = ['name', 'locality', 'slug']
    readonly_fields = ['id', 'created_at', 'updated_at']
