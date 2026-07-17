from django.contrib import admin

from .models import Booking


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = ['id', 'venue_name', 'customer_name', 'date', 'amount', 'method']
    list_filter = ['method', 'date']
    search_fields = ['id', 'venue_name', 'customer_name', 'phone']
    readonly_fields = ['id', 'created_at']
