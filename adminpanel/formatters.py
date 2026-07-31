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
from bookings.slots import now_minutes_ist, slots_end_minute, today_ist
from venues.completion import compute_completion
from venues.models import Listing, VenueDraft

from .models import AuditEntry, Payout, Review, Settings

AUDIT_LIMIT = 100


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

def _unit_of(listing):
    """The base-listing id when this row is a pitch/screen/hall sibling."""
    return str(((listing.record or {}).get('detail') or {}).get('unitOf') or '').strip()


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
    """All REAL venues — unit siblings (— Pitch 2 etc.) are excluded so the
    admin list and its counts match the actual number of venues."""
    return [
        venue_row(l)
        for l in Listing.objects.select_related('vendor').all()
        if not _unit_of(l)
    ]


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
    """ALL customer identities — including vendors/admins who also book or
    signed in through the customer app (customer identity ≠ role)."""
    from django.db.models import Q
    return [
        user_row(u)
        for u in User.objects.filter(Q(is_customer=True) | Q(role=User.Role.PUBLIC))
    ]


# --- Bookings ----------------------------------------------------

def _booking_status(booking, today):
    """Explicit admin states (refunded etc.) win; otherwise a booking is
    'completed' once its LAST SLOT HAS ENDED (IST) — time-based, not just
    date-based, so a 06:00–08:00 booking flips the same afternoon."""
    if booking.status and booking.status != 'confirmed':
        return booking.status
    if booking.date < today:
        return 'completed'
    if booking.date == today:
        end = slots_end_minute(booking.slots)
        if end and end <= now_minutes_ist():
            return 'completed'
    return 'confirmed'


def booking_row(booking, today=None):
    today = today or today_ist()
    return {
        'id': booking.id,
        'customer': booking.customer_name,
        'venue': booking.venue_name,
        'date': booking.date.isoformat(),               # machine-readable
        'createdAt': booking.created_at.isoformat(),
        'slots': booking.slots,                         # raw slot strings
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


# --- Approvals (keyed by LISTING id) -----------------------------

# listing.status -> the approval status the panel shows.
_APPROVAL_STATUS = {
    Listing.Status.LIVE: 'approved',
    Listing.Status.PAUSED: 'approved',   # paused happens after approval
    Listing.Status.PENDING: 'pending',
    Listing.Status.CHANGES: 'changes',
    Listing.Status.REJECTED: 'rejected',
}


def approval_row(listing, now=None):
    now = now or timezone.now()
    record = listing.record or {}
    detail = record.get('detail') or {}
    # The draft (same id) still holds the payout bucket + wizard completion.
    draft = VenueDraft.objects.filter(pk=listing.pk).first()
    payout_mask = ''
    completion = 100
    if draft is not None:
        payout_mask = _mask_account((draft.data or {}).get('payout') or {})
        completion = compute_completion(draft)[0]

    checks = {'photos': False, 'pricing': False, 'payout': False}
    checks.update(listing.review_checks or {})
    submitted = listing.created_at
    return {
        'id': str(listing.id),
        'name': listing.name or record.get('name') or '',
        'vendor': listing.vendor.name or listing.vendor.phone,
        'phone': str(detail.get('contactPhone') or listing.vendor.phone),
        'category': listing.category or '',
        'city': str(record.get('city') or detail.get('city') or ''),
        'area': listing.locality or '',
        'submitted': submitted.date().isoformat(),
        'waitingH': int((now - submitted).total_seconds() // 3600),
        'completion': completion,
        'status': _APPROVAL_STATUS.get(listing.status, 'pending'),
        'price': _rupees(record.get('price') or 0),
        'capacity': str(detail.get('capacity') or ''),
        'packages': str(len(detail.get('packages') or [])),
        'amenities': detail.get('amenities') or [],
        'payout': payout_mask,
        'notes': listing.review_notes,
        'checks': checks,
        'photo': record.get('image') or '',
        'photos': record.get('gallery') or [],
        'timeline': listing.review_timeline or [],
    }


def format_approvals():
    """Base listings needing (or having gone through) admin review. Unit
    siblings never appear — approving the base cascades to its family."""
    now = timezone.now()
    rows = []
    needs_review = {Listing.Status.PENDING, Listing.Status.CHANGES, Listing.Status.REJECTED}
    for listing in Listing.objects.select_related('vendor').all():
        if _unit_of(listing):
            continue
        has_history = bool(
            listing.review_timeline or listing.review_notes or listing.review_checks
        )
        if listing.status in needs_review or has_history:
            rows.append(approval_row(listing, now))
    return rows


# --- Payouts / Reviews / Audit / Settings ------------------------

def payout_row(payout):
    return {
        'id': payout.id,
        'vendor': payout.vendor,
        'period': payout.period,
        'periodStart': payout.period_start.isoformat() if payout.period_start else '',
        'periodEnd': payout.period_end.isoformat() if payout.period_end else '',
        'grossNum': payout.gross,
        'status': payout.status,
    }


def _stars(rating):
    rating = max(0, min(5, int(rating or 0)))
    return '★' * rating + '☆' * (5 - rating)


def review_row(review):
    return {
        'id': review.id,
        'venue': review.venue,
        'reviewer': review.reviewer,
        'rating': review.rating,
        'text': review.text,
        'reason': review.reason,
        'stars': _stars(review.rating),
    }


def audit_row(entry):
    return {
        'time': entry.time or entry.created_at.strftime('%d %b, %H:%M'),
        'admin': entry.admin,
        'action': entry.action,
        'target': entry.target,       # entity NAME (readable even after deletion)
        'targetId': entry.target_id,  # raw id, separate
        'change': entry.change,
    }


def settings_row(settings):
    # No 'commission' — the platform's only revenue is the flat booking fee.
    return {
        'fee': str(settings.booking_fee),
        'feeDate': settings.fee_date,
        'categories': settings.categories or [],
        'cities': settings.cities or [],
        'amenities': settings.amenities or [],
        'banners': settings.banners or [],
    }


def build_bootstrap():
    return {
        'approvals': format_approvals(),
        'venues': format_venues(),
        'vendors': format_vendors(),
        'users': format_users(),
        'bookings': format_bookings(),
        'payouts': [payout_row(p) for p in Payout.objects.all()],
        'reviews': [
            review_row(r) for r in Review.objects.filter(status=Review.Status.FLAGGED)
        ],
        'audit': [audit_row(a) for a in AuditEntry.objects.all()[:AUDIT_LIMIT]],
        'settings': settings_row(Settings.load()),
    }
