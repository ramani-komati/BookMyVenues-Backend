from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path('health', views.health, name='health'),
    path('auth/me', views.MeView.as_view(), name='me'),
    # Built-in view: POST {"refresh": "<token>"} -> new access token.
    path('auth/refresh', TokenRefreshView.as_view(), name='token-refresh'),
]
