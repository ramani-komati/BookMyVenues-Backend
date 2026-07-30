"""
Super-admin routes — mounted at /api/admin/ (separate base from the
customer/vendor app, cookie-session auth, {"detail"} error shape).
"""
from django.urls import path

from . import views

urlpatterns = [
    path('auth/login', views.AdminLoginView.as_view(), name='admin-login'),
    path('auth/verify-otp', views.AdminVerifyOtpView.as_view(), name='admin-verify-otp'),
    path('auth/logout', views.AdminLogoutView.as_view(), name='admin-logout'),
    path('bootstrap', views.AdminBootstrapView.as_view(), name='admin-bootstrap'),
    # Writes (Phase 2)
    path('approvals/<uuid:draft_id>', views.AdminApprovalUpdateView.as_view(), name='admin-approval'),
    path('venues/<uuid:listing_id>', views.AdminVenueUpdateView.as_view(), name='admin-venue'),
    path('vendors/<int:vendor_id>', views.AdminVendorUpdateView.as_view(), name='admin-vendor'),
    path('users/<int:user_id>', views.AdminUserUpdateView.as_view(), name='admin-user'),
    path('bookings/<str:booking_id>', views.AdminBookingUpdateView.as_view(), name='admin-booking'),
]
