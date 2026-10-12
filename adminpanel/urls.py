"""
Super-admin routes — mounted at /api/admin/ (separate base from the
customer/vendor app, cookie-session auth, {"detail"} error shape).
"""
from django.urls import path

from events import admin_views as event_admin_views

from . import views

urlpatterns = [
    path('auth/login', views.AdminLoginView.as_view(), name='admin-login'),
    path('auth/verify-otp', views.AdminVerifyOtpView.as_view(), name='admin-verify-otp'),
    path('auth/logout', views.AdminLogoutView.as_view(), name='admin-logout'),
    path('bootstrap', views.AdminBootstrapView.as_view(), name='admin-bootstrap'),
    # Writes (Phase 2)
    path('approvals/<uuid:listing_id>', views.AdminApprovalUpdateView.as_view(), name='admin-approval'),
    path('venues/<uuid:listing_id>', views.AdminVenueUpdateView.as_view(), name='admin-venue'),
    # Must precede the <int:vendor_id> route so 'token' is never read as an id.
    path('vendors/token', views.AdminVendorTokenView.as_view(), name='admin-vendor-token'),
    path('vendors/<int:vendor_id>', views.AdminVendorUpdateView.as_view(), name='admin-vendor'),
    path('users/<int:user_id>', views.AdminUserUpdateView.as_view(), name='admin-user'),
    path('bookings/<str:booking_id>', views.AdminBookingUpdateView.as_view(), name='admin-booking'),
    # Ticketed events — the SUPER ADMIN owns these outright (create, edit,
    # publish/unpublish, delete). Vendors have no write path; see
    # events/vendor_views.py. Static 'events' route first so the list/create
    # endpoint is never read as an event id.
    path('events', event_admin_views.AdminEventsView.as_view(), name='admin-events'),
    path(
        'events/<uuid:event_id>',
        event_admin_views.AdminEventDetailView.as_view(),
        name='admin-event',
    ),
    path(
        'events/<uuid:event_id>/orders',
        event_admin_views.AdminEventOrdersView.as_view(),
        name='admin-event-orders',
    ),
    path(
        'events/<uuid:event_id>/orders/<str:order_id>',
        event_admin_views.AdminEventOrderDetailView.as_view(),
        name='admin-event-order',
    ),
    # New models (Phase 3)
    path('uploads', views.AdminUploadView.as_view(), name='admin-upload'),
    path('settings', views.AdminSettingsView.as_view(), name='admin-settings'),
    path('payouts/<int:payout_id>', views.AdminPayoutUpdateView.as_view(), name='admin-payout'),
    path('reviews/<int:review_id>/resolve', views.AdminReviewResolveView.as_view(), name='admin-review-resolve'),
    path('audit', views.AdminAuditView.as_view(), name='admin-audit'),
]
