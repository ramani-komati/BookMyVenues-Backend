"""
Draft (wizard) routes — mounted at /api/ per the frontend contract.
Photo upload/delete routes join in Phase 3.
"""
from django.urls import path

from . import views

urlpatterns = [
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
]
