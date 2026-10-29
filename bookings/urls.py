"""
Booking routes — mounted at /api/ per the frontend contract.
"""
from django.urls import path

from reports.views import VendorExportView

from . import checkin_views, payments, ratings, vendor_views, views

urlpatterns = [
    # Vendor portal (contract 3.4, 3.7)
    path('vendors/me/dashboard', vendor_views.VendorDashboardView.as_view(), name='vendor-dashboard'),
    # Booking verification / check-in at the gate (bookings/checkin.py).
    path('vendors/me/check-ins', checkin_views.VendorCheckInView.as_view(), name='vendor-check-ins'),
    path(
        'vendors/me/check-ins/verify',
        checkin_views.VendorCheckInVerifyView.as_view(),
        name='vendor-check-in-verify',
    ),
    path('vendors/me/ratings', ratings.VendorRatingsView.as_view(), name='vendor-ratings'),
    path(
        'vendors/me/ratings/<int:rating_id>/reply',
        ratings.VendorRatingReplyView.as_view(),
        name='vendor-rating-reply',
    ),
    # Excel exports of the vendor's own bookings, earnings, events and
    # venues — scoped to the token's account (reports/vendor_reports.py).
    path('vendors/me/exports/<slug:kind>', VendorExportView.as_view(), name='vendor-export'),
    path('vendors/me/walkin-bookings', vendor_views.WalkInBookingView.as_view(), name='walkin-booking'),
    path(
        'vendors/me/bookings/<str:booking_id>/collect',
        vendor_views.CollectBookingView.as_view(),
        name='booking-collect',
    ),
    path(
        'venues/<uuid:listing_id>/availability',
        views.AvailabilityView.as_view(),
        name='venue-availability',
    ),
    path(
        'venues/<uuid:listing_id>/ratings',
        ratings.RateVenueView.as_view(),
        name='venue-rating',
    ),
    # Ratings & reviews (bookings/ratings.py). Here rather than in the events
    # and venues apps because ONE module owns every rating rule; these
    # two-segment paths are never shadowed by those apps' one-segment
    # <id_or_slug> catch-alls.
    path(
        'venues/<uuid:listing_id>/reviews',
        ratings.PublicVenueReviewsView.as_view(),
        name='venue-reviews',
    ),
    path(
        'events/<uuid:event_id>/ratings',
        ratings.RateEventView.as_view(),
        name='event-rating',
    ),
    path(
        'events/<uuid:event_id>/reviews',
        ratings.PublicEventReviewsView.as_view(),
        name='event-reviews',
    ),
    path('payments/order', payments.PaymentOrderView.as_view(), name='payment-order'),
    path('payments/verify', payments.PaymentVerifyView.as_view(), name='payment-verify'),
    path('payments/webhook', payments.RazorpayWebhookView.as_view(), name='payment-webhook'),
    path('users/me/bookings', views.MyBookingsView.as_view(), name='my-bookings'),
    path(
        'users/me/bookings/<str:booking_id>',
        views.CancelBookingView.as_view(),
        name='cancel-booking',
    ),
]
