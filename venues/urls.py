"""
Draft (wizard) routes — mounted at /api/ per the frontend contract.
Photo upload/delete routes join in Phase 3.
"""
from django.urls import path

from . import public_views, views

urlpatterns = [
    # Vendor: publish a listing from a submitted draft (contract 3.5)
    path('vendors/me/listings', views.VendorListingPublishView.as_view(), name='listing-publish'),
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
