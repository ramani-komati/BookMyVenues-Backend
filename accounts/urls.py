from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path('health', views.health, name='health'),
    path('auth/vendor/register', views.VendorRegisterView.as_view(), name='vendor-register'),
    path('auth/login', views.LoginView.as_view(), name='login'),
    path('auth/me', views.MeView.as_view(), name='me'),
    # Built-in view: POST {"refresh": "<token>"} -> new access token.
    # Lets the frontend keep users logged in without re-entering passwords.
    path('auth/refresh', TokenRefreshView.as_view(), name='token-refresh'),
]
