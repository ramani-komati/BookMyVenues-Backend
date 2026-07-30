"""
Build the super-admin panel's display shapes from our existing models.

Each entity has a single-row builder (used by the write endpoints to echo the
updated entity) and a list builder (used by bootstrap).

payouts / reviews / audit have no backing models yet (Phase 3) so bootstrap
returns them empty; settings is a sensible default (the real ₹20 fee).

Money convention: `*Num` fields are RAW ints; `price`, `payout`, `slotsAmt`
etc. are display strings shown as-is.
"""
from django.utils import timezone

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from bookings.views import BOOKING_FEE
from venues.completion import compute_completion
from venues.models import Listing, VenueDraft

CATEGORIES = ['Private Hall', 'Private Theatre', 'Open Theatre', 'Resort', 'Playzone']


def _int(value):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _rupees(value):
    return f'₹{_int(value):,}'


def _mask_account(payout):
    bank = str(payout.get('bankName') or '').strip()
    account = str(payout.get('accountNumber') or '').strip()
    if not (bank or account):
        return ''
    tail = account[-4:] if account else ''
    return f'{bank} ····{tail}'.strip()


def _first_photo(data):
    photos = (data.get('photos') or {}).get('venuePhotos') or []
    return photos[0].get('url', '') if photos else ''


# --- Venues ------------------------------------------------------

def _venue_status(listing):
    if listing.status == Listing.Status.LIVE:
        return 'live'
    if listing.status == Listing.Status.PAUSED:
        return 'paused'
    return 'draft'


def venue_row(listing):
    record = listing.record or {}
    detail = record.get('detail') or {}
    bookings = list(listing.bookings.all())
    return {
        'id': str(listing.id),
        'name': listing.name or record.get('name') or '',
        'vendor': listing.vendor.name or listing.vendor.phone,
        'category': listing.category or record.get('category') or '',
        'city': str(record.get('city') or detail.get('city') or ''),
        'area': listing.locality or record.get('locality') or '',
        'price': _rupees(record.get('price') or 0),
        'rating': 0.0,
        'bookings': len(bookings),
        'status': _venue_status(listing),
        'featured': listing.featured,
        'capacity': str(detail.get('capacity') or ''),
        'packages': str(len(detail.get('packages') or [])),
        'hours': '06:00 – 24:00',
        'addedOn': listing.created_at.strftime('%b %Y'),
        'revenueNum': sum(b.amount for b in bookings),
        'amenities': detail.get('amenities') or [],
        'photo': record.get('image') or '',
    }


def format_venues():
    return [venue_row(l) for l in Listing.objects.select_related('vendor').all()]


# --- Vendors -----------------------------------------------------

def vendor_row(vendor):
    listings = list(vendor.listings.all())
    earnings = sum(b.amount for listing in listings for b in listing.bookings.all())
    return {
        'id': vendor.id,
        'name': vendor.name or '',
        'phone': vendor.phone,
        'email': vendor.email or '',
        'venues': len(listings),
        'earningsNum': earnings,
        'joined': vendor.date_joined.strftime('%b %Y'),
        'kyc': vendor.kyc,
        'acc': 'active' if vendor.is_active else 'suspended',
        'payout': '',
    }


def format_vendors():
    return [vendor_row(v) for v in User.objects.filter(role=User.Role.VENDOR)]


# --- Users -------------------------------------------------------

def user_row(user):
    bookings = list(user.bookings.all())
    return {
        'id': user.id,
        'name': user.name or '',
        'phone': user.phone,
        'bookings': len(bookings),
        'spentNum': sum(b.amount for b in bookings),
        'lastActive': '',
        'status': 'active' if user.is_active else 'blocked',
    }


def format_users():
    return [user_row(u) for u in User.objects.filter(role=User.Role.PUBLIC)]


# --- Bookings ----------------------------------------------------

def _booking_status(booking, today):
    # Explicit admin states win; otherwise 'completed' is derived from the date.
    if booking.status and booking.status != 'confirmed':
        return booking.status
    return 'completed' if booking.date < today else 'confirmed'


def booking_row(booking, today=None):
    today = today or today_ist()
    return {
        'id': booking.id,
        'customer': booking.customer_name,
        'venue': booking.venue_name,
        'slot': f"{booking.date.strftime('%d %b')}, {booking.slots[0] if booking.slots else ''}",
        'amountNum': booking.amount,
        'method': 'Cash' if booking.walk_in else 'UPI',
        'status': _booking_status(booking, today),
        'slotsDesc': ', '.join(booking.slots),
        'slotsAmt': _rupees(booking.amount),
        'addons': ', '.join(str(a.get('name', '')) for a in (booking.addons or [])),
    }


def format_bookings():
    today = today_ist()
    return [booking_row(b, today) for b in Booking.objects.all()]


# --- Approvals ---------------------------------------------------

def approval_row(draft, now=None):
    now = now or timezone.now()
    data = draft.data or {}
    basics = data.get('basics') or {}
    location = data.get('location') or {}
    details = data.get('details') or {}
    payout = data.get('payout') or {}
    submitted = draft.submitted_at
    checks = {'photos': False, 'pricing': False, 'payout': False}
    checks.update(draft.review_checks or {})
    return {
        'id': str(draft.id),
        'name': basics.get('venueName') or '',
        'vendor': draft.vendor.name or draft.vendor.phone,
        'phone': basics.get('phone') or draft.vendor.phone,
        'category': details.get('primaryCategory') or '',
        'city': location.get('city') or location.get('district') or '',
        'area': location.get('locality') or '',
        'submitted': submitted.strftime('%d %b') if submitted else '',
        'waitingH': int((now - submitted).total_seconds() // 3600) if submitted else 0,
        'completion': compute_completion(draft)[0],
        'status': draft.review_status,
        'price': _rupees(details.get('price') or 0),
        'capacity': str(details.get('capacity') or ''),
        'packages': str(len(details.get('packages') or [])),
        'amenities': details.get('amenities') or [],
        'payout': _mask_account(payout),
        'notes': draft.review_notes,
        'checks': checks,
        'photo': _first_photo(data),
        'timeline': draft.review_timeline or [],
    }


def format_approvals():
    now = timezone.now()
    pending = VenueDraft.objects.filter(
        status=VenueDraft.Status.PENDING
    ).select_related('vendor')
    return [approval_row(draft, now) for draft in pending]


# --- Settings ----------------------------------------------------

def default_settings():
    return {
        'fee': str(BOOKING_FEE),
        'feeDate': '',
        'commission': '10',
        'categories': CATEGORIES,
        'cities': [],
        'amenities': [],
        'banners': [],
    }


def build_bootstrap():
    return {
        'approvals': format_approvals(),
        'venues': format_venues(),
        'vendors': format_vendors(),
        'users': format_users(),
        'bookings': format_bookings(),
        'payouts': [],
        'reviews': [],
        'audit': [],
        'settings': default_settings(),
    }
