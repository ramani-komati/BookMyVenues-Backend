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
]
