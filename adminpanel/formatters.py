"""
Build the super-admin panel's display shapes from our existing models.

Each entity has a single-row builder (used by the write endpoints to echo the
updated entity) and a list builder (used by bootstrap).

payouts / reviews / audit have no backing models yet (Phase 3) so bootstrap
returns them empty; settings is a sensible default (the real ₹20 fee).

Money convention: `*Num` fields are RAW ints; `price`, `payout`, `slotsAmt`
etc. are display strings shown as-is.
"""
from collections import defaultdict

from django.utils import timezone

from accounts.models import User, vendor_accounts_q
from bookings.models import Booking, Rating
from bookings.part_payment import read_config as read_part_payment
from bookings.payment_record import payment_time_fields
from bookings.slots import now_minutes_ist, slots_end_minute, today_ist
from venues.completion import compute_completion
from venues.models import Listing, VenueDraft

from .models import AuditEntry, Payout, Review, Settings

AUDIT_LIMIT = 100


class BootstrapContext:
    """Precomputed lookups for the bootstrap read.

    Every row builder used to query per row (ratings, bookings, drafts,
    siblings) — 58 queries for 7 venues, and growing linearly with the
    catalogue. This loads each table ONCE and groups in Python, so bootstrap
    is a fixed handful of queries no matter how big the platform gets.
    Single-row builders (used by the write endpoints) still work with
    ctx=None and fall back to their original per-row queries.
    """

    def __init__(self):
        self.listings = list(Listing.objects.select_related('vendor').all())

        # Unit siblings fold into their base venue (same rule as ratings).
        self.base_of = {}
        for listing in self.listings:
            unit_of = str(
                ((listing.record or {}).get('detail') or {}).get('unitOf') or ''
            ).strip()
            self.base_of[str(listing.id)] = unit_of or str(listing.id)

        self.listings_by_vendor = defaultdict(list)
        for listing in self.listings:
            self.listings_by_vendor[listing.vendor_id].append(listing)

        self.bookings_by_listing = defaultdict(list)
        self.bookings_by_user = defaultdict(list)
        # Soft-removed bookings are gone as far as the panel is concerned:
        # no row, no booking count, no revenue.
        self.bookings = list(
            Booking.objects.exclude(status='removed').select_related('rating')
        )
        for booking in self.bookings:
            if booking.listing_id:
                self.bookings_by_listing[str(booking.listing_id)].append(booking)
            if booking.user_id:
                self.bookings_by_user[booking.user_id].append(booking)

        # Ratings aggregated per venue SET (base id -> (average, count)).
        totals = defaultdict(lambda: [0, 0])
        for listing_id, stars in Rating.objects.values_list('listing_id', 'stars'):
            entry = totals[self.base_of.get(str(listing_id), str(listing_id))]
            entry[0] += stars
            entry[1] += 1
        self.rating_by_base = {
            base: (round(total / count, 4), count)
            for base, (total, count) in totals.items()
        }

        self.drafts = {str(draft.id): draft for draft in VenueDraft.objects.all()}

    def rating_for(self, listing):
        base = self.base_of.get(str(listing.id), str(listing.id))
        return self.rating_by_base.get(base, (None, 0))

    def bookings_for(self, listing):
        return self.bookings_by_listing.get(str(listing.id), [])

    def draft_for(self, listing_id):
        return self.drafts.get(str(listing_id))


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


def _draft_location(listing_id, ctx=None):
    """The wizard's location bucket for this id ({} when no draft exists) —
    the fallback source for district/city on rows published before the
    frontend started sending them flat on the record."""
    draft = (
        ctx.draft_for(listing_id) if ctx is not None
        else VenueDraft.objects.filter(pk=listing_id).first()
    )
    return ((draft.data or {}).get('location') or {}) if draft else {}


def _district_and_city(listing, ctx=None):
    record = listing.record or {}
    detail = record.get('detail') or {}
    district = str(record.get('district') or detail.get('district') or '').strip()
    city = str(record.get('city') or detail.get('city') or '').strip()
    if not district or not city:
        location = _draft_location(listing.pk, ctx)
        district = district or str(location.get('district') or '').strip()
        city = city or str(
            location.get('city') or location.get('district') or ''
        ).strip()
    return district, city


def _venue_status(listing):
    # A pending deletion request is reported to the ADMIN as its own status,
    # while the venue's real status stays live/paused so customers can still
    # book it until a decision is made.
    if listing.deletion_requested_at and listing.status != Listing.Status.DELETED:
        return 'deletion_requested'
    if listing.status == Listing.Status.LIVE:
        return 'live'
    if listing.status == Listing.Status.PAUSED:
        return 'paused'
    if listing.status == Listing.Status.DELETED:
        return 'deleted'
    return 'draft'


def venue_row(listing, ctx=None):
    record = listing.record or {}
    detail = record.get('detail') or {}
    if ctx is not None:
        bookings = ctx.bookings_for(listing)
        average, _count = ctx.rating_for(listing)
    else:
        from bookings.ratings import venue_rating
        bookings = list(listing.bookings.exclude(status='removed'))
        average, _count = venue_rating(listing)
    district, city = _district_and_city(listing, ctx)
    from venues.taxonomy import category_group
    from venues.timings import timings_label, validate_timings, TimingsError
    try:
        timings = validate_timings(detail.get('timings'))
    except TimingsError:
        timings = None
    category = listing.category or record.get('category') or ''
    return {
        'id': str(listing.id),
        'name': listing.name or record.get('name') or '',
        'vendor': listing.vendor.name or listing.vendor.phone,
        'category': category,
        # The primary category it browses under ('celebrate', 'sports', …).
        'categoryGroup': category_group(category),
        'city': city,
        'district': district,
        'area': listing.locality or record.get('locality') or '',
        'price': _rupees(record.get('price') or 0),
        'rating': average or 0.0,
        'bookings': len(bookings),
        'status': _venue_status(listing),
        'featured': listing.featured,
        'capacity': str(detail.get('capacity') or ''),
        # Per-venue part payment, normalised: the panel and the customer app
        # both read it, and a malformed config reads back as None (= off)
        # rather than a shape either of them has to defend against.
        'partPayment': read_part_payment(record),
        'packages': str(len(detail.get('packages') or [])),
        # The venue's saved Venue Timings (was a fixed '06:00 – 24:00' for
        # every venue). Weekly hours set under Business controls are more
        # specific and are shown to the vendor there.
        'hours': timings_label(timings) or 'Not set (bookable 6:00 AM – 12:00 AM)',
        'timings': timings,
        'addedOn': listing.created_at.strftime('%b %Y'),
        'deletedAt': listing.deleted_at.isoformat() if listing.deleted_at else None,
        'deletionRequestedAt': (
            listing.deletion_requested_at.isoformat()
            if listing.deletion_requested_at else None
        ),
        'revenueNum': sum(b.amount for b in bookings),
        'amenities': detail.get('amenities') or [],
        'photo': record.get('image') or '',
    }


def format_venues(ctx):
    """All REAL venues — unit siblings (— Pitch 2 etc.) are excluded so the
    admin list and its counts match the actual number of venues."""
    return [venue_row(l, ctx) for l in ctx.listings if not _unit_of(l)]


# --- Vendors -----------------------------------------------------

def vendor_row(vendor, ctx=None):
    if ctx is not None:
        listings = ctx.listings_by_vendor.get(vendor.id, [])
        earnings = sum(
            b.amount for listing in listings for b in ctx.bookings_for(listing)
        )
    else:
        listings = list(vendor.listings.all())
        earnings = sum(
            b.amount for listing in listings
            for b in listing.bookings.exclude(status='removed')
        )
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


def format_vendors(ctx):
    return [vendor_row(v, ctx) for v in User.objects.filter(vendor_accounts_q())]


# --- Users -------------------------------------------------------

def user_row(user, ctx=None):
    bookings = (
        ctx.bookings_by_user.get(user.id, []) if ctx is not None
        else list(user.bookings.exclude(status='removed'))
    )
    return {
        'id': user.id,
        'name': user.name or '',
        'email': user.email or '',
        'phone': user.phone,
        'bookings': len(bookings),
        'spentNum': sum(b.amount for b in bookings),
        'lastActive': '',
        'status': 'active' if user.is_active else 'blocked',
    }


def format_users(ctx):
    """ALL customer identities — including vendors/admins who also book or
    signed in through the customer app (customer identity ≠ role)."""
    from django.db.models import Q
    return [
        user_row(u, ctx)
        for u in User.objects.filter(Q(is_customer=True) | Q(role=User.Role.PUBLIC))
    ]


# --- Bookings ----------------------------------------------------

def _booking_status(booking, today=None):
    """Explicit admin states (refunded etc.) win; otherwise a booking is
    'completed' once its LAST SLOT HAS ENDED (IST) — time-based, not just
    date-based, so a 06:00–08:00 booking flips the same afternoon. One
    definition, shared with the customer/vendor record via the model."""
    return booking.display_status


def booking_removal_block(booking):
    """(code, message) when this venue booking must NOT be removed, else None.

    The same financial rule event orders follow (events.admin_views.
    removal_block): only a booking that never took money may go.
      * confirmed / refund_pending — it holds a slot and money may be held;
        refunding is a separate, deliberate act.
      * refunded — the money moved twice; that row IS the financial record.
      * any row carrying a captured payment id — a customer can cancel a
        PAID booking, and that row must stay until the refund is settled.
      * payment_pending inside its hold — the customer may be paying now.
    """
    if booking.status in ('confirmed', 'refund_pending'):
        return ('ACTIVE', 'This booking is confirmed or paid, so it cannot be '
                          'deleted. Refund it first.')
    if booking.status == 'refunded':
        return ('REFUNDED', 'Refunded bookings are kept as a financial record.')
    if booking.status not in Booking.REMOVABLE_STATUSES:
        return ('NOT_REMOVABLE', 'This booking cannot be deleted.')
    if booking.razorpay_payment_id:
        return ('PAID', 'A payment is recorded against this booking, so it '
                        'cannot be deleted.')
    if booking.status == 'payment_pending' and not booking.hold_expired:
        return ('IN_CHECKOUT', 'The customer may still be paying. Try again '
                               'once the 30-minute payment hold has expired.')
    return None


def booking_row(booking, today=None):
    today = today or today_ist()
    blocked = booking_removal_block(booking)
    return {
        'id': booking.id,
        'customer': booking.customer_name,
        'venue': booking.venue_name,
        'date': booking.date.isoformat(),               # machine-readable
        'createdAt': booking.created_at.isoformat(),
        'slots': booking.slots,                         # raw slot strings
        'slot': f"{booking.date.strftime('%d %b')}, {booking.slots[0] if booking.slots else ''}",
        'amountNum': booking.amount,
        'fee': booking.fee,                             # fee actually charged
        'phone': booking.phone or '',
        'sport': booking.sport or None,
        'unit': booking.unit,
        'unitLabel': booking.unit_label or None,
        'refundReason': booking.refund_reason,
        'refundAmount': booking.refund_amount,
        'offer': booking.offer or None,        # {code,title,type,value,source}
        'discountAmount': booking.discount_amount,
        'method': booking.method,   # echoed exactly as stored (upi/card/…/venue/walk-in)
        'occasion': booking.occasion or None,
        'occasionNote': booking.occasion_note or None,
        'collected': booking.collected,
        'collectedAt': booking.collected_at.isoformat() if booking.collected_at else None,
        'status': _booking_status(booking, today),
        # The raw persisted lifecycle value, as event orders report it:
        # `status` derives 'completed' from the clock, this never guesses.
        'paymentStatus': booking.status,
        'razorpayPaymentId': booking.razorpay_payment_id,
        # Booking Info (the expanded row): the invoice number, the gateway
        # order, and when the payment actually succeeded — distinct from
        # createdAt above. Event orders carry the same keys (as_record).
        'razorpayOrderId': booking.razorpay_order_id,
        'invoiceNumber': booking.invoice_number or None,
        **payment_time_fields(booking),
        # An unpaid hold past its window is an abandoned checkout.
        'holdExpired': booking.hold_expired,
        # Which kind of unpaid row: 'held' (5-minute slot hold, checkout
        # open), 'payment_pending' (payment started) or 'expired' — and when
        # the hold ends (bookings/holds.py). Null on every other row.
        'holdState': booking.hold_state,
        'holdExpiresAt': (
            booking.hold_expiry.isoformat()
            if booking.status == 'payment_pending' and booking.hold_expiry else None
        ),
        # Booking status and payment status as two answers (the badges the
        # Bookings page shows); `status`/`paymentStatus` above are unchanged.
        **booking.state_fields(),
        'removable': blocked is None,
        'removeBlockedReason': blocked[1] if blocked else '',
        'slotsDesc': ', '.join(booking.slots),
        'slotsAmt': _rupees(booking.amount),
        'addons': ', '.join(str(a.get('name', '')) for a in (booking.addons or [])),
        # Additive: 'slot' | 'membership' | 'package' | 'surprise', plus the
        # membership validity / package guests / surprise message (offerings app).
        'kind': booking.kind or 'slot',
        'offering': booking.offering_summary(),
    }


def format_bookings(ctx):
    today = today_ist()
    return [booking_row(b, today) for b in ctx.bookings]


# --- Approvals (keyed by LISTING id) -----------------------------

# listing.status -> the approval status the panel shows.
_APPROVAL_STATUS = {
    Listing.Status.LIVE: 'approved',
    Listing.Status.PAUSED: 'approved',   # paused happens after approval
    Listing.Status.PENDING: 'pending',
    Listing.Status.CHANGES: 'changes',
    Listing.Status.REJECTED: 'rejected',
}


def approval_row(listing, now=None, ctx=None):
    now = now or timezone.now()
    record = listing.record or {}
    detail = record.get('detail') or {}
    district, city = _district_and_city(listing, ctx)
    # The draft (same id) still holds the payout bucket + wizard completion.
    draft = (
        ctx.draft_for(listing.pk) if ctx is not None
        else VenueDraft.objects.filter(pk=listing.pk).first()
    )
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
        'city': city,
        'district': district,
        'area': listing.locality or '',
        'submitted': submitted.date().isoformat(),
        'waitingH': int((now - submitted).total_seconds() // 3600),
        'completion': completion,
        'status': _APPROVAL_STATUS.get(listing.status, 'pending'),
        'price': _rupees(record.get('price') or 0),
        'capacity': str(detail.get('capacity') or ''),
        # Per-venue part payment, normalised: the panel and the customer app
        # both read it, and a malformed config reads back as None (= off)
        # rather than a shape either of them has to defend against.
        'partPayment': read_part_payment(record),
        'packages': str(len(detail.get('packages') or [])),
        'amenities': detail.get('amenities') or [],
        'payout': payout_mask,
        'notes': listing.review_notes,
        'checks': checks,
        'photo': record.get('image') or '',
        'photos': record.get('gallery') or [],
        'timeline': listing.review_timeline or [],
    }


def format_approvals(ctx):
    """Base listings needing (or having gone through) admin review. Unit
    siblings never appear — approving the base cascades to its family."""
    now = timezone.now()
    rows = []
    needs_review = {Listing.Status.PENDING, Listing.Status.CHANGES, Listing.Status.REJECTED}
    for listing in ctx.listings:
        if _unit_of(listing) or listing.status == Listing.Status.DELETED:
            continue   # deleted venues leave the approvals queue
        has_history = bool(
            listing.review_timeline or listing.review_notes or listing.review_checks
        )
        if listing.status in needs_review or has_history:
            rows.append(approval_row(listing, now, ctx))
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
    # The timestamp ALWAYS comes from the server clock — a client-sent time
    # gave us rows frozen at the literal "Just now", and client clocks and
    # timezones can't be trusted in an audit trail.
    return {
        'time': entry.created_at.strftime('%d %b, %H:%M'),
        'createdAt': entry.created_at.isoformat(),
        'admin': entry.admin,
        'action': entry.action,
        'target': entry.target,       # entity NAME (readable even after deletion)
        'targetId': entry.target_id,  # raw id, separate
        'change': entry.change,
    }


# --- Events ------------------------------------------------------

def _paid_seats_for_date(event_date):
    """Capacity units taken by PAID orders on one date (no unpaid holds)."""
    from django.db.models import Sum

    from events.models import EventOrder

    return EventOrder.objects.filter(event_date=event_date).exclude(
        status__in=(*EventOrder.DEAD_STATUSES, 'payment_pending')
    ).aggregate(total=Sum('seats'))['total'] or 0


def event_row(event, sold_by_date=None, order_stats=None):
    """One event for the admin Events page.

    `sold_by_date` / `order_stats` are the precomputed maps from
    format_events — pass them when rendering a LIST, or every row costs a
    query per date. Omitted (single-row echo after a write) they are counted
    on the spot, which is one extra query for one event.

    "Sold" here means PAID, not held: unpaid Razorpay holds are checkout
    plumbing and would make the admin's numbers jump around. The customer's
    availability read counts them (see events/inventory.seats_sold) because
    a held seat genuinely is not buyable — the two answer different
    questions on purpose.
    """
    dates = list(event.dates.all())
    rows = []
    for event_date in dates:
        sold = (
            sold_by_date.get(event_date.id, 0)
            if sold_by_date is not None
            else _paid_seats_for_date(event_date)
        )
        rows.append({
            'id': event_date.id,
            'date': event_date.date.isoformat(),
            'capacity': event_date.capacity,
            'unlimited': event_date.capacity is None,
            'sold': sold,
            'remaining': (
                None if event_date.capacity is None else max(0, event_date.capacity - sold)
            ),
            'isAvailable': event_date.is_available,
        })

    if order_stats is not None:
        tickets, revenue, orders = order_stats.get(str(event.id), (0, 0, 0))
    else:
        from events.models import EventOrder
        confirmed = EventOrder.objects.filter(event=event, status='confirmed')
        tickets = sum(o.ticket_count for o in confirmed)
        revenue = sum(o.subtotal for o in confirmed)
        orders = confirmed.count()

    prices = [t.price for t in event.ticket_types.all() if t.is_active]
    return {
        'id': str(event.id),
        'slug': event.slug,
        'name': event.name,
        'category': event.category,
        'status': event.status,
        # Events are admin-created now and carry NO vendor. `vendor` stays in
        # the payload (the panel renders a "who runs it" column) but reads the
        # admin-entered organiser, falling back to the vendor account only on
        # the legacy rows that still have one.
        'vendor': event.organizer,
        'organizer': event.organizer_name,
        'organizerPhone': event.organizer_phone,
        'vendorId': event.vendor_id,
        'vendorPhone': event.vendor.phone if event.vendor_id else '',
        'managedBy': 'vendor' if event.vendor_id else 'admin',
        'poster': event.poster_url,
        'location': event.location,
        'city': event.city,
        # Homepage promotion — separate from `status` (see Event.featured).
        'featured': event.featured,
        'featuredAt': event.featured_at.isoformat() if event.featured_at else None,
        'startTime': event.start_time.strftime('%H:%M') if event.start_time else '',
        'endTime': event.end_time.strftime('%H:%M') if event.end_time else '',
        'dates': rows,
        'datesCount': len(rows),
        # None when any night is unlimited (a total would be meaningless).
        'capacity': (
            None if any(r['capacity'] is None for r in rows)
            else sum(r['capacity'] for r in rows)
        ),
        'unlimited': any(r['capacity'] is None for r in rows),
        'ticketsSold': tickets,
        'ordersNum': orders,
        'revenueNum': revenue,
        'fromPriceNum': min(prices) if prices else 0,
        'ticketTypes': [t.as_record() for t in event.ticket_types.all()],
        'reviewNotes': event.review_notes,
        'createdAt': event.created_at.isoformat(),
        'submitted': event.created_at.strftime('%d %b %Y'),
        # The rest of the editable record. The admin Events page is now a full
        # editor, so the row has to carry everything the form binds to —
        # without these a reopened event would silently blank its own
        # description, address and terms on the next save.
        'description': event.description,
        'slugValue': event.slug,
        'venueId': str(event.listing_id) if event.listing_id else None,
        'venueName': event.venue_name,
        'address': event.address,
        'locality': event.locality,
        'mapsLink': event.maps_link,
        'contactPhone': event.contact_phone,
        'terms': event.terms,
        'gallery': list(event.gallery or []),
        'bookingOpensOn': (
            event.booking_opens_on.isoformat() if event.booking_opens_on else None
        ),
        'bookingClosesOn': (
            event.booking_closes_on.isoformat() if event.booking_closes_on else None
        ),
    }


def format_events():
    """Every non-deleted event, with sold counts gathered in two queries."""
    from events.models import Event, EventOrder

    events = list(
        Event.objects.exclude(status=Event.Status.DELETED)
        .select_related('vendor')
        .prefetch_related('dates', 'ticket_types')
    )
    if not events:
        return []

    # Seats held per date, and per-event confirmed totals — one pass each,
    # mirroring how BootstrapContext avoids a query per row.
    sold_by_date = defaultdict(int)
    order_stats = defaultdict(lambda: [0, 0, 0])
    live = EventOrder.objects.exclude(
        status__in=EventOrder.DEAD_STATUSES
    ).values_list('event_id', 'event_date_id', 'status', 'seats',
                  'ticket_count', 'subtotal')
    for event_id, date_id, order_status, seats, tickets, subtotal in live:
        if date_id and order_status != 'payment_pending':
            sold_by_date[date_id] += seats or 0
        if event_id and order_status == 'confirmed':
            stats = order_stats[str(event_id)]
            stats[0] += tickets or 0
            stats[1] += subtotal or 0
            stats[2] += 1

    frozen = {key: tuple(value) for key, value in order_stats.items()}
    return [event_row(e, dict(sold_by_date), frozen) for e in events]


def format_event_orders():
    """Every event ticket order for the main Bookings page.

    Kept as its OWN key rather than mixed into `bookings`: the dashboard,
    payouts and venue/user drawers all sum `bookings` as venue revenue, and
    an event ticket must not leak into those figures. Soft-removed orders are
    left out exactly as the per-event list leaves them out; every other
    status (pending, failed, cancelled, refunds) is listed as persisted.
    """
    from events.admin_views import admin_order_record
    from events.models import EventOrder

    orders = (
        EventOrder.objects.exclude(status='removed')
        .select_related('event', 'user')
    )
    return [admin_order_record(order) for order in orders]


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
    ctx = BootstrapContext()   # one pass over the data, then pure Python
    return {
        'approvals': format_approvals(ctx),
        'venues': format_venues(ctx),
        'vendors': format_vendors(ctx),
        'users': format_users(ctx),
        'bookings': format_bookings(ctx),
        'payouts': [payout_row(p) for p in Payout.objects.all()],
        'reviews': [
            review_row(r) for r in Review.objects.filter(status=Review.Status.FLAGGED)
        ],
        'audit': [audit_row(a) for a in AuditEntry.objects.all()[:AUDIT_LIMIT]],
        'settings': settings_row(Settings.load()),
        # Ticketed events (Navaratri, Dandiya…). CREATED AND OWNED by the
        # super admin — see events/admin_views.py. Legacy vendor-submitted
        # rows still appear here so their moderation queue can be cleared.
        'events': format_events(),
        # Event ticket orders, listed beside venue bookings on the Bookings
        # page. Separate from 'bookings' on purpose — see format_event_orders.
        'eventOrders': format_event_orders(),
    }
