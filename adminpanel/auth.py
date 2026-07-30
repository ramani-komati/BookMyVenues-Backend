"""
Auth building blocks for the super-admin panel.

The admin panel uses a COOKIE SESSION (not the Bearer JWT the customer/vendor
app uses). The frontend calls with `credentials: "include"` and does NOT send a
CSRF token, so we use a CSRF-exempt session authentication for the decoupled
admin SPA. Cross-site protection then rests on the CORS origin allowlist and the
SameSite/Secure cookie flags — tighten to a CSRF-token exchange before opening
the admin panel to untrusted networks.
"""
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from accounts.models import User


class CsrfExemptSessionAuthentication(SessionAuthentication):
    """Session auth without DRF's CSRF check (the admin SPA sends no token)."""

    def enforce_csrf(self, request):
        return  # intentionally skip


class IsAdmin(BasePermission):
    """Allow only a logged-in user whose role is ADMIN."""

    message = 'Admin access only.'

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role == User.Role.ADMIN
        )


def detail(text, http_status):
    """The admin error shape: {"detail": "..."}."""
    return Response({'detail': text}, status=http_status)
