from django.contrib import admin

from .models import Event, EventDate, EventOrder, TicketType


class EventDateInline(admin.TabularInline):
    model = EventDate
    extra = 0


class TicketTypeInline(admin.TabularInline):
    model = TicketType
    extra = 0


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'organizer', 'status', 'created_at')
    list_filter = ('status', 'category')
    search_fields = ('name', 'slug', 'venue_name', 'city', 'organizer_name')
    inlines = [EventDateInline, TicketTypeInline]


@admin.register(EventOrder)
class EventOrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'event_name', 'date', 'ticket_count', 'amount', 'status')
    list_filter = ('status',)
    search_fields = ('id', 'event_name', 'phone', 'razorpay_order_id')
    # Orders are financial records — read-only in the Django admin; status
    # changes belong in the super-admin panel where they are audited.
    readonly_fields = [f.name for f in EventOrder._meta.fields]
