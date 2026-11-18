"""
OTP auth routes — mounted at /api/ (the frontend's base URL).
Matches the frontend contract exactly (Groups 2 & 3 auth).
"""
from django.urls import path

from . import vendor_settings, views

urlpatterns = [
    # Customers
    path('users/auth/otp', views.UserOTPRequestView.as_view(), name='user-otp'),
    path('users/auth/verify', views.UserOTPVerifyView.as_view(), name='user-verify'),
    path('users/me', views.UserProfileUpdateView.as_view(), name='user-profile'),
    # Vendors
    path('vendors/auth/otp', views.VendorOTPRequestView.as_view(), name='vendor-otp'),
    path('vendors/auth/verify', views.VendorOTPVerifyView.as_view(), name='vendor-verify'),
    path('vendors/me', views.VendorProfileUpdateView.as_view(), name='vendor-profile'),
    # Business contact details + alert preferences (accounts/vendor_settings.py).
    path('vendors/me/settings', vendor_settings.VendorSettingsView.as_view(), name='vendor-settings'),
    path('vendors', views.VendorRegisterView.as_view(), name='vendor-register'),
]
