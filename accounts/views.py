from django.contrib.auth import authenticate
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import UserSerializer, VendorRegisterSerializer


def _auth_payload(user):
    """Build the {token, refresh, user} response used by register and login."""
    refresh = RefreshToken.for_user(user)
    return {
        'token': str(refresh.access_token),  # short-lived access token (1 day)
        'refresh': str(refresh),             # long-lived refresh token (30 days)
        'user': UserSerializer(user).data,
    }


@api_view(['GET'])
@permission_classes([AllowAny])
def health(request):
    """GET /api/v1/health — lets the frontend/Render check the API is alive."""
    return Response({'status': 'ok'})


class VendorRegisterView(APIView):
    """POST /api/v1/auth/vendor/register — create a VENDOR account."""

    permission_classes = [AllowAny]
    # Rate limit (settings.py) — blocks bots mass-creating accounts.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = VendorRegisterSerializer(data=request.data)
        if not serializer.is_valid():
            # Field-level errors, e.g. {"phone": ["...10 digits."]} -> 400
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.save()
        return Response(_auth_payload(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    """POST /api/v1/auth/login — log in with {phone, password}."""

    permission_classes = [AllowAny]
    # Rate limit — blocks password guessing (brute force).
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'auth'

    def post(self, request):
        phone = request.data.get('phone')
        password = request.data.get('password')

        if not phone or not password:
            return Response(
                {'detail': 'Phone and password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # authenticate() checks the hashed password and that the
        # account is active. Returns None on any failure — we never
        # reveal WHICH part was wrong (safer against attackers).
        user = authenticate(request, phone=phone, password=password)
        if user is None:
            return Response(
                {'detail': 'Invalid phone or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response(_auth_payload(user), status=status.HTTP_200_OK)


class MeView(APIView):
    """GET /api/v1/auth/me — who am I? (requires Bearer token)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # request.user is set automatically by JWT authentication.
        return Response(UserSerializer(request.user).data)
