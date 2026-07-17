from django.contrib import admin

from .models import Addon, Package, PayoutDetails, SportPricing, Unit, Venue, VenueDraft, VenuePhoto

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
    list_display = ('user', 'account_holder', 'bank_name', 'ifsc')
    search_fields = ('user__phone', 'account_holder')


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
