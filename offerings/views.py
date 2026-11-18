"""
Memberships, resort packages and big screen surprises — the HTTP side.

Public
  GET  /api/offerings/options                    the forms' option lists
  GET  /api/venues/<id>/offerings?date=          what a venue sells + what's left on `date`
Customer (user JWT)
  POST /api/offerings/quote                      server price for a selection
  POST /api/offerings/order                      Razorpay order + held booking
  POST /api/offerings/surprise-media             upload a surprise photo / video
  GET  /api/users/me/memberships                 the customer's memberships
Vendor (vendor JWT, own venues only)
  GET  /api/vendors/me/memberships               members of the vendor's gyms
  POST /api/vendors/me/memberships/<id>/visits   record today's visit

Payment confirmation is the EXISTING /api/payments/verify + webhook: every
purchase is a bookings.Booking, so nothing about money is duplicated here.
"""
import logging
import uuid

from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import F
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from bookings.models import Booking
from bookings.holds import hold_payload, new_expiry
from bookings.razorpay_client import RazorpayError, create_order
from bookings.slots import parse_date, today_ist
from venues.models import Listing
from venues.views import IsVendor

from . import availability, catalog, checkout
from .models import FitnessPlan, Membership, MembershipVisit, OfferingProfile, ResortPackage, SurpriseShow
from .summary import membership_payload, membership_state

logger = logging.getLogger(__name__)


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


class OptionsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response(catalog.option_lists())


class VenueOfferingsView(APIView):
    """What a live venue sells. With ?date= the packages' slots and the
    surprise shows carry live availability (uncached on purpose — a stale
    "available" would be a lie, as for slot availability)."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request, listing_id):
        listing = Listing.objects.filter(pk=listing_id, status=Listing.Status.LIVE).first()
        profile = OfferingProfile.objects.filter(listing=listing).first() if listing else None
        if profile is None:
            return _message('This venue has no memberships, packages or surprises.', status.HTTP_404_NOT_FOUND)

        date = None
        if request.query_params.get('date'):
            try:
                date = parse_date(request.query_params.get('date'))
            except Exception:
                return _message('date must be YYYY-MM-DD.', status.HTTP_400_BAD_REQUEST)
        settings = profile.settings or {}
        payload = {'kind': profile.kind, 'settings': settings, 'date': date.isoformat() if date else None}

        if profile.kind == 'fitness':
            plans = FitnessPlan.objects.filter(listing=listing, enabled=True, archived_at__isnull=True)
            payload['plans'] = [checkout.plan_public(p) for p in plans]
            payload['returningMember'] = checkout.is_returning_member(request.user, listing)
        elif profile.kind == 'resort':
            packages = ResortPackage.objects.filter(listing=listing, enabled=True, archived_at__isnull=True)
            rows = []
            for package in packages:
                row = checkout.package_public(package)
                if date:
                    row['availability'] = availability.package_slot_rows(package, settings, date)
                rows.append(row)
            payload['packages'] = rows
        else:
            shows = SurpriseShow.objects.filter(listing=listing, enabled=True, archived_at__isnull=True)
            rows = []
            for show in shows:
                row = checkout.show_public(show)
                if date:
                    problem = availability.show_date_problem(settings, show, date)
                    if problem and show.available_to and date > show.available_to:
                        continue          # the show's run is over — not "unavailable"
                    row['availability'] = availability.show_time_rows(listing, settings, show, date)
                rows.append(row)
            payload['shows'] = rows
            payload['dateProblem'] = (
                availability.date_problem(settings, date, window=(
                    availability._iso(settings.get('availableFrom')), availability._iso(settings.get('availableTo'))))
                if date else None
            )
        return Response(payload)


class QuoteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        result, error = checkout.validate({**request.data, 'amount': None}, request.user)
        if error is not None:
            return error
        return Response(checkout.quote_payload(result))


class OrderView(APIView):
    """Price → Razorpay order → held booking (status payment_pending), exactly
    like POST /api/payments/order does for slots. The customer then pays in
    the Razorpay widget and the app calls POST /api/payments/verify."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        result, error = checkout.validate(request.data, request.user)
        if error is not None:
            return error
        try:
            order_id = create_order(result.amount, receipt=request.user.phone)
        except RazorpayError as exc:
            return _message(str(exc), status.HTTP_502_BAD_GATEWAY)

        listing = result.listing
        with transaction.atomic():
            Listing.objects.select_for_update().get(pk=listing.pk)
            try:
                result.recheck()
            except checkout.CheckoutError as problem:
                return _message(problem.message, problem.http_status)
            record = listing.record or {}
            booking = Booking.objects.create(
                listing=listing, user=request.user, kind=result.kind,
                venue_name=record.get('name') or listing.name, category=listing.category,
                location=str(record.get('location') or ''), image=str(record.get('image') or ''),
                customer_name=str(request.data.get('customer') or request.user.name or ''),
                phone=request.user.phone, date=result.date, slots=result.slots,
                unit_label=result.unit_label, per_slot=0, addons=result.lines,
                fee=result.fee, amount=result.amount, method=Booking.Method.ONLINE,
                pay_now=result.amount, at_venue=0, occasion=result.occasion[:80],
                status='payment_pending', razorpay_order_id=order_id,
                # The same 5-minute hold as slot bookings (bookings/holds.py).
                hold_expires_at=new_expiry(),
            )
            result.create_detail(booking)

        return Response({
            'orderId': order_id,
            'keyId': django_settings.RAZORPAY_KEY_ID,
            'amount': result.amount * 100,
            'payNow': result.amount,
            'atVenue': 0,
            'bookingId': booking.id,
            'hold': hold_payload(booking),
            'quote': checkout.quote_payload(result),
        }, status=status.HTTP_201_CREATED)


# --- surprise media -----------------------------------------------------------

MEDIA_TYPES = {
    'photo': {'image/jpeg': 'jpg', 'image/png': 'png', 'image/webp': 'webp'},
    'video': {'video/mp4': 'mp4', 'video/webm': 'webm', 'video/quicktime': 'mov'},
}
MEDIA_LIMITS = {'photo': 5 * 1024 * 1024, 'video': 30 * 1024 * 1024}


def _looks_like(kind, head):
    if kind == 'photo':
        return head.startswith(b'\xff\xd8\xff') or head.startswith(b'\x89PNG\r\n\x1a\n') or (
            head[:4] == b'RIFF' and head[8:12] == b'WEBP')
    # MP4 / MOV carry 'ftyp' at byte 4; WebM starts with the EBML magic.
    return head[4:8] in (b'ftyp', b'moov', b'mdat', b'wide') or head.startswith(b'\x1a\x45\xdf\xa3')


class SurpriseMediaView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payments'

    def post(self, request):
        kind = str(request.data.get('type') or '').strip()
        if kind not in MEDIA_TYPES:
            return _message('type must be "photo" or "video".', status.HTTP_400_BAD_REQUEST)
        listing = Listing.objects.filter(pk=str(request.data.get('venueId') or ''), status=Listing.Status.LIVE).first() \
            if _is_uuid(request.data.get('venueId')) else None
        profile = OfferingProfile.objects.filter(listing=listing, kind='surprise').first() if listing else None
        if profile is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)
        if not (profile.settings or {}).get('allowPhoto' if kind == 'photo' else 'allowVideo'):
            return _message(f'This theatre does not accept a {kind}.', status.HTTP_400_BAD_REQUEST)

        file = request.FILES.get('file')
        if file is None:
            return _message('No file uploaded.', status.HTTP_400_BAD_REQUEST)
        if file.content_type not in MEDIA_TYPES[kind]:
            allowed = 'JPEG, PNG or WebP' if kind == 'photo' else 'MP4, WebM or MOV'
            return _message(f'Only {allowed} files are allowed.', status.HTTP_400_BAD_REQUEST)
        if file.size > MEDIA_LIMITS[kind]:
            return _message(f'That {kind} is too large (max {MEDIA_LIMITS[kind] // (1024 * 1024)} MB).',
                            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        head = file.read(12)
        file.seek(0)
        if not _looks_like(kind, head):
            return _message(f'That file is not a valid {kind}.', status.HTTP_400_BAD_REQUEST)

        from venues.storage import StorageError, upload_photo
        path = f'surprises/{request.user.id}/{uuid.uuid4().hex}.{MEDIA_TYPES[kind][file.content_type]}'
        try:
            url = upload_photo(path, file.read(), file.content_type)
        except StorageError:
            logger.exception('Surprise media upload failed')
            return _message('Could not store the file right now. Please try again.', status.HTTP_502_BAD_GATEWAY)
        return Response({'url': url, 'type': kind}, status=status.HTTP_201_CREATED)


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


# --- memberships --------------------------------------------------------------

def _membership_row(membership, *, for_vendor=False):
    booking = membership.booking
    row = {
        'id': booking.id,
        'venueId': str(membership.listing_id) if membership.listing_id else None,
        'venueName': booking.venue_name,
        'amount': booking.amount,
        'fee': booking.fee,
        'invoiceNumber': booking.invoice_number or None,
        'paidAt': booking.paid_at.isoformat() if booking.paid_at else None,
        'createdAt': booking.created_at.isoformat(),
        'paymentStatus': booking.status,
        **membership_payload(membership, booking),
    }
    if for_vendor:
        row['customer'] = booking.customer_name
        row['phone'] = booking.phone or None
        row['lastVisit'] = (
            membership.visits.values_list('visited_on', flat=True).first()
        )
        if row['lastVisit']:
            row['lastVisit'] = row['lastVisit'].isoformat()
    return row


class MyMembershipsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = Membership.objects.filter(user=request.user).select_related('booking').exclude(
            booking__status__in=('payment_pending', 'failed', 'cancelled', 'removed'),
        )
        return Response({'memberships': [_membership_row(m) for m in rows]})


class VendorMembershipsView(APIView):
    permission_classes = [IsVendor]

    def get(self, request):
        rows = Membership.objects.filter(listing__vendor=request.user).select_related('booking').exclude(
            booking__status__in=('payment_pending', 'failed', 'cancelled', 'removed'),
        )
        venue = request.query_params.get('venueId')
        if venue and _is_uuid(venue):
            rows = rows.filter(listing_id=venue)
        return Response({'memberships': [_membership_row(m, for_vendor=True) for m in rows[:500]]})


class VendorMembershipVisitView(APIView):
    """Record that a member came in today. One visit per member per day;
    only a paid membership inside its validity with visits left."""

    permission_classes = [IsVendor]

    def post(self, request, booking_id):
        today = today_ist()
        with transaction.atomic():
            membership = Membership.objects.select_for_update().select_related('booking').filter(
                booking_id=booking_id, listing__vendor=request.user,
            ).first()
            if membership is None:
                return _message('Membership not found.', status.HTTP_404_NOT_FOUND)
            state = membership_state(membership, membership.booking.status, today)
            if state == 'upcoming':
                return _message(f'This membership starts on {membership.starts_on:%d %b %Y}.', status.HTTP_409_CONFLICT)
            if state != 'active':
                return _message(f'This membership is not active ({state.replace("_", " ")}).', status.HTTP_409_CONFLICT)
            if membership.visits_allowed is not None and membership.visits_used >= membership.visits_allowed:
                return _message('No visits left on this membership.', status.HTTP_409_CONFLICT)
            if membership.visits.filter(visited_on=today).exists():
                return _message('A visit is already recorded for today.', status.HTTP_409_CONFLICT)
            MembershipVisit.objects.create(membership=membership, visited_on=today, recorded_by=request.user)
            Membership.objects.filter(pk=membership.pk).update(visits_used=F('visits_used') + 1)
            membership.refresh_from_db()
        return Response({'membership': _membership_row(membership, for_vendor=True)})
