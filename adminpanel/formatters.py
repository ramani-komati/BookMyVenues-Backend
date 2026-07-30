"""
Build the super-admin panel's display shapes from our existing models.

Phase 1: venues / vendors / users / bookings / approvals come from real data;
payouts / reviews / audit have no backing models yet (Phase 3) so they are
empty, and settings is a sensible default (the real ₹20 fee).

Money convention: `*Num` fields are RAW ints (for maths/sorting); `price`,
`payout`, `slotsAmt` etc. are display strings shown as-is.
"""
from django.utils import timezone

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from bookings.views import BOOKING_FEE
from venues.completion import compute_completion
from venues.models import Listing, VenueDraft

# Category labels the settings tab offers (from the wizard's categories).
CATEGORIES = ['Private Hall', 'Private Theatre', 'Open Theatre', 'Resort', 'Playzone']


def _int(value):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _rupees(value):
    return f'₹{_int(value):,}'


def _mask_account(payout):
    """'HDFC Bank ····4821' from the payout bucket, or '' when empty."""
    bank = str(payout.get('bankName') or '').strip()
    account = str(payout.get('accountNumber') or '').strip()
    if not (bank or account):
        return ''
    tail = account[-4:] if account else ''
    return f'{bank} ····{tail}'.strip()


def _first_photo(data):
    photos = (data.get('photos') or {}).get('venuePhotos') or []
    return photos[0].get('url', '') if photos else ''


def format_venues():
    rows = []
    for listing in Listing.objects.select_related('vendor').all():
        record = listing.record or {}
        detail = record.get('detail') or {}
        bookings = list(listing.bookings.all())
        rows.append({
            'id': str(listing.id),
            'name': listing.name or record.get('name') or '',
            'vendor': listing.vendor.name or listing.vendor.phone,
            'category': listing.category or record.get('category') or '',
            'city': str(record.get('city') or detail.get('city') or ''),
            'area': listing.locality or record.get('locality') or '',
            'price': _rupees(record.get('price') or 0),
            'rating': 0.0,
            'bookings': len(bookings),
            'status': 'live' if listing.status == Listing.Status.LIVE else 'draft',
            'featured': False,
            'capacity': str(detail.get('capacity') or ''),
            'packages': str(len(detail.get('packages') or [])),
            'hours': '06:00 – 24:00',
            'addedOn': listing.created_at.strftime('%b %Y'),
            'revenueNum': sum(b.amount for b in bookings),
            'amenities': detail.get('amenities') or [],
            'photo': record.get('image') or '',
        })
    return rows


def format_vendors():
    rows = []
    for vendor in User.objects.filter(role=User.Role.VENDOR):
        listings = list(vendor.listings.all())
        earnings = sum(
            b.amount for listing in listings for b in listing.bookings.all()
        )
        rows.append({
            'id': vendor.id,
            'name': vendor.name or '',
            'phone': vendor.phone,
            'email': vendor.email or '',
            'venues': len(listings),
            'earningsNum': earnings,
            'joined': vendor.date_joined.strftime('%b %Y'),
            'kyc': 'pending',   # no KYC field yet (Phase 2)
            'acc': 'active' if vendor.is_active else 'suspended',
            'payout': '',
        })
    return rows


def format_users():
    rows = []
    for user in User.objects.filter(role=User.Role.PUBLIC):
        bookings = list(user.bookings.all())
        rows.append({
            'id': user.id,
            'name': user.name or '',
            'phone': user.phone,
            'bookings': len(bookings),
            'spentNum': sum(b.amount for b in bookings),
            'lastActive': '',
            'status': 'active' if user.is_active else 'blocked',
        })
    return rows


def format_bookings():
    today = today_ist()
    rows = []
    for booking in Booking.objects.all():
        rows.append({
            'id': booking.id,
            'customer': booking.customer_name,
            'venue': booking.venue_name,
            'slot': f"{booking.date.strftime('%d %b')}, {booking.slots[0] if booking.slots else ''}",
            'amountNum': booking.amount,
            'method': 'Cash' if booking.walk_in else 'UPI',
            'status': 'completed' if booking.date < today else 'confirmed',
            'slotsDesc': ', '.join(booking.slots),
            'slotsAmt': _rupees(booking.amount),
            'addons': ', '.join(str(a.get('name', '')) for a in (booking.addons or [])),
        })
    return rows


def format_approvals():
    now = timezone.now()
    rows = []
    pending = VenueDraft.objects.filter(
        status=VenueDraft.Status.PENDING
    ).select_related('vendor')
    for draft in pending:
        data = draft.data or {}
        basics = data.get('basics') or {}
        location = data.get('location') or {}
        details = data.get('details') or {}
        payout = data.get('payout') or {}
        submitted = draft.submitted_at
        rows.append({
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
            'status': 'pending',
            'price': _rupees(details.get('price') or 0),
            'capacity': str(details.get('capacity') or ''),
            'packages': str(len(details.get('packages') or [])),
            'amenities': details.get('amenities') or [],
            'payout': _mask_account(payout),
            'notes': '',
            'checks': {'photos': False, 'pricing': False, 'payout': False},
            'photo': _first_photo(data),
            'timeline': [],
        })
    return rows


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
    """The single aggregate payload the panel loads on start."""
    return {
        'approvals': format_approvals(),
        'venues': format_venues(),
        'vendors': format_vendors(),
        'users': format_users(),
        'bookings': format_bookings(),
        'payouts': [],   # no Payout model yet (Phase 3)
        'reviews': [],   # no Review model yet (Phase 3)
        'audit': [],     # no AuditEntry model yet (Phase 3)
        'settings': default_settings(),
    }
