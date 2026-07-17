"""
OTP auth routes — mounted at /api/ (the frontend's base URL).
Matches the frontend contract exactly (Groups 2 & 3 auth).
"""
from django.urls import path

from . import views

urlpatterns = [
    # Customers
    path('users/auth/otp', views.UserOTPRequestView.as_view(), name='user-otp'),
    path('users/auth/verify', views.UserOTPVerifyView.as_view(), name='user-verify'),
    # Vendors
    path('vendors/auth/otp', views.VendorOTPRequestView.as_view(), name='vendor-otp'),
    path('vendors/auth/verify', views.VendorOTPVerifyView.as_view(), name='vendor-verify'),
    path('vendors', views.VendorRegisterView.as_view(), name='vendor-register'),
]
