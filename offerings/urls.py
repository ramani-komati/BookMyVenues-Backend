"""Offerings routes — mounted at /api/ (see offerings/views.py)."""
from django.urls import path

from . import views

urlpatterns = [
    path('offerings/options', views.OptionsView.as_view(), name='offering-options'),
    path('offerings/quote', views.QuoteView.as_view(), name='offering-quote'),
    path('offerings/order', views.OrderView.as_view(), name='offering-order'),
    path('offerings/surprise-media', views.SurpriseMediaView.as_view(), name='offering-surprise-media'),
    path('venues/<uuid:listing_id>/offerings', views.VenueOfferingsView.as_view(), name='venue-offerings'),
    path('users/me/memberships', views.MyMembershipsView.as_view(), name='my-memberships'),
    path('vendors/me/memberships', views.VendorMembershipsView.as_view(), name='vendor-memberships'),
    path(
        'vendors/me/memberships/<str:booking_id>/visits',
        views.VendorMembershipVisitView.as_view(),
        name='vendor-membership-visit',
    ),
]
