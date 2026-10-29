"""
Event routes — mounted at /api/ alongside the booking and venue routes.

Ordering matters: the static /events/quote and /events/payments/* paths are
declared BEFORE the <str:id_or_slug> catch-all so they are never read as an
event slug (the same trap venues/urls.py notes for /venues/drafts).
"""
from django.urls import path

from . import customer_views, payments, public_views, vendor_views

urlpatterns = [
    # --- Vendor portal (Vendor JWT): submit events for admin approval ---
    path('vendors/me/events', vendor_views.VendorEventsView.as_view(), name='vendor-events'),
    path(
        'vendors/me/event-images',
        vendor_views.VendorEventImageUploadView.as_view(),
        name='vendor-event-images',
    ),
    path(
        'vendors/me/events/<uuid:event_id>',
        vendor_views.VendorEventDetailView.as_view(),
        name='vendor-event-detail',
    ),
    path(
        'vendors/me/events/<uuid:event_id>/poster',
        vendor_views.VendorEventPosterView.as_view(),
        name='vendor-event-poster',
    ),
    path(
        'vendors/me/events/<uuid:event_id>/orders',
        vendor_views.VendorEventOrdersView.as_view(),
        name='vendor-event-orders',
    ),

    # --- Customer tickets (User JWT) ---
    path(
        'users/me/event-orders',
        customer_views.MyEventOrdersView.as_view(),
        name='my-event-orders',
    ),
    path(
        'users/me/event-orders/<str:order_id>',
        customer_views.CancelEventOrderView.as_view(),
        name='cancel-event-order',
    ),

    # --- Checkout (User JWT). Static segments BEFORE the slug catch-all. ---
    path('events/quote', payments.EventQuoteView.as_view(), name='event-quote'),
    path(
        'events/payments/order',
        payments.EventPaymentOrderView.as_view(),
        name='event-payment-order',
    ),
    path(
        'events/payments/verify',
        payments.EventPaymentVerifyView.as_view(),
        name='event-payment-verify',
    ),

    # --- Public browsing (no auth) ---
    path('events', public_views.PublicEventListView.as_view(), name='public-event-list'),
    path(
        'events/<uuid:event_id>/availability',
        public_views.PublicEventAvailabilityView.as_view(),
        name='public-event-availability',
    ),
    # MUST stay last — it matches any single segment under /events/.
    path(
        'events/<str:id_or_slug>',
        public_views.PublicEventDetailView.as_view(),
        name='public-event-detail',
    ),
]
