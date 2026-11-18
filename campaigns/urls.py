"""
Public campaign routes — mounted at /api/ (see BookMyVenue/urls.py). The
super-admin routes live with the rest of the panel in adminpanel/urls.py.
"""
from django.urls import path

from . import public_views

urlpatterns = [
    path('users/me/email-preferences', public_views.EmailPreferencesView.as_view(),
         name='user-email-preferences'),
    path('email/unsubscribe', public_views.UnsubscribeView.as_view(), name='email-unsubscribe'),
    path('email/webhooks/resend', public_views.ResendWebhookView.as_view(),
         name='email-webhook-resend'),
]
