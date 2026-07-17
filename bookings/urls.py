"""
Booking routes — mounted at /api/ per the frontend contract.
"""
from django.urls import path

from . import vendor_views, views

urlpatterns = [
    # Vendor portal (contract 3.4, 3.7)
    path('vendors/me/dashboard', vendor_views.VendorDashboardView.as_view(), name='vendor-dashboard'),
    path('vendors/me/walkin-bookings', vendor_views.WalkInBookingView.as_view(), name='walkin-booking'),
    path(
        'venues/<uuid:listing_id>/availability',
        views.AvailabilityView.as_view(),
        name='venue-availability',
    ),
    path('users/me/bookings', views.MyBookingsView.as_view(), name='my-bookings'),
    path(
        'users/me/bookings/<str:booking_id>',
        views.CancelBookingView.as_view(),
        name='cancel-booking',
    ),
]
