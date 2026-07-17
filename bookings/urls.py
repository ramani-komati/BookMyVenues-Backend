"""
Booking routes — mounted at /api/ per the frontend contract.
"""
from django.urls import path

from . import views

urlpatterns = [
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
