"""
Draft (wizard) routes — mounted at /api/ per the frontend contract.
Photo upload/delete routes join in Phase 3.
"""
from django.urls import path

from . import business_views, favorites, geocoding, maps, public_views, views

urlpatterns = [
    # Google Maps short-link resolver (P2) — public, no auth.
    path('maps/resolve', maps.MapsResolveView.as_view(), name='maps-resolve'),
    # Customer location picker: place search / locality name (public; OFF
    # until a provider is configured — see venues/geocoding.py).
    path('geo/search', geocoding.PlaceSearchView.as_view(), name='geo-search'),
    path('geo/reverse', geocoding.PlaceReverseView.as_view(), name='geo-reverse'),
    # Customer wishlist sync (User JWT).
    path('users/me/favorites', favorites.FavoritesView.as_view(), name='favorites'),
    # Vendor: publish / delete listings (contract 3.5, 3.6)
    path('vendors/me/listings', views.VendorListingPublishView.as_view(), name='listing-publish'),
    path(
        'vendors/me/listings/<uuid:listing_id>',
        views.VendorListingDeleteView.as_view(),
        name='listing-delete',
    ),
    path(
        'vendors/me/listings/<uuid:listing_id>/deletion-request',
        views.VendorListingDeletionRequestView.as_view(),
        name='listing-deletion-request',
    ),
    # Vendor business controls: hours, closures, price rules, blocked slots
    # and the availability calendar (venues/business_views.py).
    path(
        'vendors/me/venues/<uuid:listing_id>/business',
        business_views.VendorBusinessView.as_view(),
        name='vendor-business',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/business/schedule',
        business_views.VendorScheduleView.as_view(),
        name='vendor-business-schedule',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/business/schedule/preview',
        business_views.VendorSchedulePreviewView.as_view(),
        name='vendor-business-schedule-preview',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/business/pricing',
        business_views.VendorPricingView.as_view(),
        name='vendor-business-pricing',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/business/pricing/preview',
        business_views.VendorPricingPreviewView.as_view(),
        name='vendor-business-pricing-preview',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/calendar',
        business_views.VendorCalendarView.as_view(),
        name='vendor-calendar',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/blocks',
        business_views.VendorBlocksView.as_view(),
        name='vendor-blocks',
    ),
    path(
        'vendors/me/venues/<uuid:listing_id>/blocks/<int:block_id>',
        business_views.VendorBlockDetailView.as_view(),
        name='vendor-block-detail',
    ),
    path('venues/drafts', views.DraftCreateView.as_view(), name='draft-create'),
    path('venues/drafts/<uuid:draft_id>', views.DraftDetailView.as_view(), name='draft-detail'),
    path(
        'venues/drafts/<uuid:draft_id>/sections/<str:section>',
        views.DraftSectionView.as_view(),
        name='draft-section',
    ),
    path('venues/drafts/<uuid:draft_id>/photos', views.DraftPhotoUploadView.as_view(), name='draft-photo-upload'),
    path(
        'venues/drafts/<uuid:draft_id>/photos/<str:photo_id>',
        views.DraftPhotoDeleteView.as_view(),
        name='draft-photo-delete',
    ),
    path('venues/drafts/<uuid:draft_id>/submit', views.DraftSubmitView.as_view(), name='draft-submit'),
    path('venues/drafts/<uuid:draft_id>/reopen', views.DraftReopenView.as_view(), name='draft-reopen'),
    path('venues/drafts/<uuid:draft_id>/seed', views.DraftSeedView.as_view(), name='draft-seed'),
    # Public browsing (contract 1.1, 1.2). The catch-all <id_or_slug>
    # route MUST stay last so it never shadows /venues/drafts.
    path('venues', public_views.PublicVenueListView.as_view(), name='public-venue-list'),
    path('venues/<str:id_or_slug>', public_views.PublicVenueDetailView.as_view(), name='public-venue-detail'),
]
