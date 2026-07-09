from django.urls import path

from . import views

urlpatterns = [
    path('vendor/venues', views.VenueListCreateView.as_view(), name='venue-list-create'),
    path('vendor/venues/<int:pk>', views.VenueDetailView.as_view(), name='venue-detail'),
    path('vendor/venues/<int:pk>/submit', views.VenueSubmitView.as_view(), name='venue-submit'),
    path('vendor/venues/<int:pk>/photos', views.VenuePhotoCreateView.as_view(), name='venue-photo-create'),
    path('vendor/venues/<int:pk>/photos/<int:photo_id>', views.VenuePhotoDeleteView.as_view(), name='venue-photo-delete'),
    path('vendor/payout', views.PayoutView.as_view(), name='payout'),
]
