"""
Super-admin exports — GET /api/admin/exports/<kind>.

Each export lists the records its panel page lists, with that page's own
filters applied HERE (the page sends them as query parameters):

  kind        page                      filters
  ---------   -----------------------   ----------------------------------
  users       Users                     q, status (active | blocked)
  vendors     Vendors                   q, acc (active | suspended),
                                        kyc (pending | verified | rejected)
  venues      Live venues / All venues  scope (live | all), status
  approvals   Venue approvals           status, category, city
  events      Events                    tab (live | promoted | draft |
                                        paused | pending | rejected | all)
  payouts     Payouts                   status (pending | completed | failed)
  refunds     Refunds                   tab (requests | paused | history)
  payments    Payouts → Payments report from, to (day the booking was made, IST)
  audit       Audit log                 —

A page with pagination (Approvals) exports every matching row, not the page
on screen. Passwords, OTP hashes, tokens and full bank details are never read
into an export: users and vendors are exported field by field, and the payout
account appears only in the masked form the Approvals page already shows.
"""
from collections import defaultdict

from django.db.models import Q
from django.utils import timezone

from accounts.models import User, vendor_accounts_q
from adminpanel.formatters import (
    BootstrapContext,
    _district_and_city,
    _int,
    _unit_of,
    _venue_status,
    approval_row,
)
from adminpanel.models import AuditEntry, Payout
from bookings.models import Booking
from events.models import Event, EventOrder
from venues.models import Listing

from . import common as c
from .sheets import event_sheets
from .workbook import DATE, DATETIME, INT, MONEY, Cell, Sheet, col

REPORTS = {}


class Report:
    def __init__(self, kind, prefix, label, noun, parse, build):
        self.kind = kind
        self.prefix = prefix      # file name: <prefix>_<YYYY-MM-DD>.xlsx
        self.label = label        # audit: "Exported <label>"
        self.noun = noun          # audit: "<count> <noun>"
        self.parse = parse
        self.build = build        # filters -> (sheets, row count)


def report(kind, prefix, label, noun, parse):
    def register(build):
        REPORTS[kind] = Report(kind, prefix, label, noun, parse, build)
        return build
    return register


def _family(listings):
    """{listing id: base listing id} — a unit sibling ("Arena — Pitch 2")
    folds into its base venue, as the panel counts venues."""
    return {str(l.id): _unit_of(l) or str(l.id) for l in listings}


ROLE_LABEL = {
    User.Role.PUBLIC: 'Customer',
    User.Role.VENDOR: 'Vendor',
    User.Role.ADMIN: 'Admin',
}
PANEL_VENUE_STATUS = {
    'live': 'Live', 'paused': 'Paused', 'deletion_requested': 'Deletion requested',
    'draft': 'Draft', 'deleted': 'Deleted',
}
APPROVAL_LABEL = {
    Listing.Status.LIVE: 'Approved',
    Listing.Status.PAUSED: 'Approved',
    Listing.Status.PENDING: 'Pending review',
    Listing.Status.CHANGES: 'Changes requested',
    Listing.Status.REJECTED: 'Rejected',
    Listing.Status.DELETED: 'Deleted',
}


# =====================================================================
# Users
# =====================================================================

def users_filters(params):
    return {
        'q': c.query(params),
        'status': c.choice(params, 'status', ('All', 'active', 'blocked'), 'All'),
    }


@report('users', 'admin_users', 'users', 'users', users_filters)
def users_report(f):
    # The Users page's set: every customer identity, including vendors and
    # admins who also book through the customer app (formatters.format_users).
    users = User.objects.filter(Q(is_customer=True) | Q(role=User.Role.PUBLIC))
    if f['status'] == 'active':
        users = users.filter(is_active=True)
    elif f['status'] == 'blocked':
        users = users.filter(is_active=False)
    users = [
        u for u in users.order_by('-date_joined', '-id')
        # The page's search box reads exactly these fields.
        if c.matches(f['q'], [u.id, u.name, u.email, u.phone])
    ]
    c.check_size(len(users))
    wanted = {u.id for u in users}

    # [listed, kept, kept value, refunded] per user — one pass, no per-row query.
    venue = defaultdict(lambda: [0, 0, 0, 0])
    for user_id, status, amount in Booking.objects.exclude(status='removed').filter(
        user__isnull=False
    ).values_list('user_id', 'status', 'amount'):
        if user_id not in wanted:
            continue
        entry = venue[user_id]
        entry[0] += 1
        if c.is_kept(status):
            entry[1] += 1
            entry[2] += amount
        if status == 'refunded':
            entry[3] += 1
    event = defaultdict(lambda: [0, 0, 0])
    for user_id, status, amount in EventOrder.objects.exclude(status='removed').filter(
        user__isnull=False
    ).values_list('user_id', 'status', 'amount'):
        if user_id not in wanted:
            continue
        entry = event[user_id]
        if status == 'confirmed':
            entry[0] += 1
            entry[1] += amount
        if status == 'refunded':
            entry[2] += 1

    columns = [
        col('User ID', 9, INT),
        col('Name', 24),
        col('Email', 30),
        col('Phone', 14),
        col('Role', 11),
        col('Vendor Access', 10),
        col('Account Status', 13),
        col('Registration Date', 18, DATETIME),
        col('Venue Bookings (listed)', 12, INT),
        col('Confirmed Venue Bookings', 12, INT),
        col('Confirmed Venue Booking Value', 16, MONEY),
        col('Paid Event Orders', 11, INT),
        col('Paid Event Order Value', 16, MONEY),
        col('Refunded Bookings & Orders', 12, INT),
    ]

    def rows():
        for u in users:
            v, e = venue.get(u.id, (0, 0, 0, 0)), event.get(u.id, (0, 0, 0))
            yield [
                u.id, u.name, u.email or '', u.phone,
                ROLE_LABEL.get(u.role, u.role), u.has_vendor_access,
                'Active' if u.is_active else 'Blocked', u.date_joined,
                v[0], v[1], v[2], e[0], e[1], v[3] + e[2],
            ]

    return [Sheet('Users', columns, rows())], len(users)


# =====================================================================
# Vendors
# =====================================================================

def vendors_filters(params):
    return {
        'q': c.query(params),
        'acc': c.choice(params, 'acc', ('All', 'active', 'suspended'), 'All'),
        'kyc': c.choice(params, 'kyc', ('All', 'pending', 'verified', 'rejected'), 'All'),
    }


@report('vendors', 'admin_vendors', 'vendors', 'vendors', vendors_filters)
def vendors_report(f):
    vendors = User.objects.filter(vendor_accounts_q())
    if f['acc'] == 'active':
        vendors = vendors.filter(is_active=True)
    elif f['acc'] == 'suspended':
        vendors = vendors.filter(is_active=False)
    if f['kyc'] != 'All':
        vendors = vendors.filter(kyc=f['kyc'])
    vendors = [
        v for v in vendors.order_by('-date_joined', '-id')
        if c.matches(f['q'], [v.id, v.name, v.email, v.phone])
    ]
    c.check_size(len(vendors))
    wanted = {v.id for v in vendors}

    # Every listing once (the catalogue is small next to bookings); filtered
    # in Python so a long vendor list never becomes one giant IN clause.
    listings = [
        l for l in Listing.objects.only('id', 'vendor_id', 'name', 'record', 'status')
        if l.vendor_id in wanted
    ]
    venues = defaultdict(lambda: [0, 0, []])   # count, live, names
    owner = {}
    for listing in listings:
        owner[str(listing.id)] = listing.vendor_id
        if _unit_of(listing) or listing.status == Listing.Status.DELETED:
            continue
        entry = venues[listing.vendor_id]
        entry[0] += 1
        if listing.status == Listing.Status.LIVE:
            entry[1] += 1
        name = listing.name or (listing.record or {}).get('name') or ''
        if name:
            entry[2].append(name)

    # [kept, gross, fees, net, refund pending, refunded, refunded ₹]
    money = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0])
    bookings = Booking.objects.exclude(status='removed').filter(listing__isnull=False)
    for booking in bookings.only(
        'listing_id', 'status', 'amount', 'fee', 'discount_amount', 'offer',
        'refund_amount', 'razorpay_payment_id', 'pay_now',
    ).iterator(chunk_size=2000):
        vendor_id = owner.get(str(booking.listing_id))
        if vendor_id is None:
            continue
        entry = money[vendor_id]
        if c.is_kept(booking.status):
            entry[0] += 1
            entry[1] += booking.amount
            entry[2] += booking.fee
            entry[3] += c.vendor_net(booking)
        elif booking.status == 'refund_pending':
            entry[4] += 1
        elif booking.status == 'refunded':
            entry[5] += 1
            amount, _basis = c.refund_figures(
                booking.status, booking.refund_amount, c.venue_captured(booking)
            )
            entry[6] += amount or 0
    events = defaultdict(int)
    for vendor_id in Event.objects.exclude(status=Event.Status.DELETED).filter(
        vendor_id__isnull=False
    ).values_list('vendor_id', flat=True):
        events[vendor_id] += 1

    columns = [
        col('Vendor ID', 10, INT),
        col('Name', 24),
        col('Phone', 14),
        col('Email', 30),
        col('Role', 10),
        col('Account Status', 13),
        col('KYC Status', 11),
        col('Registration Date', 18, DATETIME),
        col('Venues (excl. deleted)', 11, INT),
        col('Live Venues', 9, INT),
        col('Venue Names', 40),
        col('Events Submitted', 10, INT),
        col('Kept Bookings', 10, INT),
        col('Gross Booking Value (kept)', 16, MONEY),
        col('Platform Fees (kept)', 14, MONEY),
        col('Vendor Net Earnings', 15, MONEY),
        col('Refund Pending Bookings', 11, INT),
        col('Refunded Bookings', 10, INT),
        col('Refunded Amount', 13, MONEY),
    ]

    def rows():
        for v in vendors:
            count, live, names = venues.get(v.id, (0, 0, []))
            m = money.get(v.id, (0, 0, 0, 0, 0, 0, 0))
            yield [
                v.id, v.name, v.phone, v.email or '',
                'Admin' if v.role == User.Role.ADMIN else 'Vendor',
                'Active' if v.is_active else 'Suspended',
                str(v.kyc).capitalize(), v.date_joined,
                count, live, ', '.join(sorted(set(names))), events.get(v.id, 0),
                *m,
            ]

    return [Sheet('Vendors', columns, rows())], len(vendors)


# =====================================================================
# Venues (Live venues / All venues)
# =====================================================================

VENUE_STATUSES = ('All', 'live', 'paused', 'deletion_requested', 'pending',
                  'rejected', 'draft', 'deleted')


def venues_filters(params):
    scope = c.choice(params, 'scope', ('all', 'live'), 'all')
    status = c.choice(params, 'status', VENUE_STATUSES, 'All')
    return {'scope': scope, 'status': status if scope == 'all' else 'All'}


@report('venues', 'admin_venues', 'venues', 'venues', venues_filters)
def venues_report(f):
    ctx = BootstrapContext()
    family = _family(ctx.listings)
    rows_for = defaultdict(list)
    for booking in ctx.bookings:
        if booking.listing_id:
            rows_for[family.get(str(booking.listing_id), str(booking.listing_id))].append(booking)
    units = defaultdict(int)
    for listing in ctx.listings:
        if _unit_of(listing):
            units[_unit_of(listing)] += 1

    selected = []
    for listing in ctx.listings:
        if _unit_of(listing):
            continue      # unit siblings fold into their venue, as on the page
        status = _venue_status(listing)
        if f['scope'] == 'live' and status not in ('live', 'deletion_requested'):
            continue
        if f['status'] != 'All' and status != f['status']:
            continue
        selected.append((listing, status))
    c.check_size(len(selected))

    columns = [
        col('Venue ID', 38),
        col('Venue Name', 30),
        col('Category', 16),
        col('Vendor', 22),
        col('Vendor Phone', 14),
        col('District', 14),
        col('City', 14),
        col('Area', 18),
        col('Listed Price (₹/hr)', 13, MONEY),
        col('Capacity', 10),
        col('Panel Status', 16),
        col('Approval Status', 17),
        col('Featured', 9),
        col('Rating', 8, '0.0'),
        col('Ratings', 8, INT),
        col('Unit Listings', 9, INT),
        col('Kept Bookings (all units)', 12, INT),
        col('Gross Booking Value (kept)', 16, MONEY),
        col('Added On', 18, DATETIME),
        col('Deletion Requested At', 18, DATETIME),
        col('Deleted At', 18, DATETIME),
    ]

    def rows():
        for listing, status in selected:
            record = listing.record or {}
            detail = record.get('detail') or {}
            district, city = _district_and_city(listing, ctx)
            average, count = ctx.rating_for(listing)
            kept = [b for b in rows_for.get(str(listing.id), []) if c.is_kept(b.status)]
            yield [
                str(listing.id),
                listing.name or record.get('name') or '',
                listing.category or record.get('category') or '',
                listing.vendor.name or listing.vendor.phone,
                listing.vendor.phone,
                district, city,
                listing.locality or record.get('locality') or '',
                _int(record.get('price') or 0),
                str(detail.get('capacity') or ''),
                PANEL_VENUE_STATUS.get(status, status),
                APPROVAL_LABEL.get(listing.status, listing.status),
                listing.featured,
                average, count,
                units.get(str(listing.id), 0),
                len(kept), sum(b.amount for b in kept),
                listing.created_at,
                listing.deletion_requested_at,
                listing.deleted_at,
            ]

    return [Sheet('Venues', columns, rows())], len(selected)


# =====================================================================
# Venue approvals
# =====================================================================

def approvals_filters(params):
    def text(name):
        value = str(params.get(name) or 'All').strip()
        if len(value) > c.MAX_QUERY_LENGTH:
            raise c.ExportFilterError(f'Unknown {name} filter.')
        return value
    return {
        'status': c.choice(params, 'status',
                           ('All', 'pending', 'changes', 'approved', 'rejected'), 'All'),
        'category': text('category'),
        'city': text('city'),
    }


@report('approvals', 'admin_approvals', 'venue approvals', 'submissions', approvals_filters)
def approvals_report(f):
    ctx = BootstrapContext()
    now = timezone.now()
    needs_review = {Listing.Status.PENDING, Listing.Status.CHANGES, Listing.Status.REJECTED}
    selected = []
    # formatters.format_approvals' selection, then the page's three filters.
    for listing in ctx.listings:
        if _unit_of(listing) or listing.status == Listing.Status.DELETED:
            continue
        has_history = bool(
            listing.review_timeline or listing.review_notes or listing.review_checks
        )
        if not (listing.status in needs_review or has_history):
            continue
        row = approval_row(listing, now, ctx)
        if f['status'] != 'All' and row['status'] != f['status']:
            continue
        if f['category'] != 'All' and row['category'] != f['category']:
            continue
        if f['city'] != 'All' and f['city'] not in (row['district'], row['city']):
            continue
        selected.append((listing, row))
    selected.sort(key=lambda pair: pair[0].created_at, reverse=True)
    c.check_size(len(selected))

    label = {'pending': 'Pending', 'changes': 'Changes requested',
             'approved': 'Approved', 'rejected': 'Rejected'}
    columns = [
        col('Listing ID', 38),
        col('Venue Name', 30),
        col('Vendor', 22),
        col('Contact Phone', 14),
        col('Category', 16),
        col('District', 14),
        col('City', 14),
        col('Area', 18),
        col('Submitted', 13, DATE),
        col('Waiting (hours)', 10, INT),
        col('Completion (%)', 10, INT),
        col('Approval Status', 17),
        col('Listed Price (₹/hr)', 13, MONEY),
        col('Capacity', 10),
        col('Packages', 9, INT),
        col('Payout Account (masked)', 20),
        col('Photos Checked', 9),
        col('Pricing Checked', 9),
        col('Payout Checked', 9),
        col('Review Notes', 40),
    ]

    def rows():
        for listing, row in selected:
            checks = row['checks']
            yield [
                row['id'], row['name'], row['vendor'], row['phone'], row['category'],
                row['district'], row['city'], row['area'], listing.created_at.date(),
                row['waitingH'], row['completion'], label.get(row['status'], row['status']),
                _int((listing.record or {}).get('price') or 0), row['capacity'],
                _int(row['packages']), row['payout'],
                bool(checks.get('photos')), bool(checks.get('pricing')),
                bool(checks.get('payout')), row['notes'],
            ]

    return [Sheet('Approvals', columns, rows())], len(selected)


# =====================================================================
# Events
# =====================================================================

EVENT_TABS = ('all', 'live', 'promoted', 'draft', 'paused', 'pending', 'rejected')


def events_filters(params):
    return {'tab': c.choice(params, 'tab', EVENT_TABS, 'all')}


@report('events', 'admin_events', 'events', 'events', events_filters)
def events_report(f):
    events = Event.objects.exclude(status=Event.Status.DELETED).select_related(
        'vendor'
    ).prefetch_related('dates', 'ticket_types')
    tab = f['tab']
    if tab == 'promoted':
        events = events.filter(featured=True, status=Event.Status.LIVE)
    elif tab != 'all':
        events = events.filter(status=tab)
    rank = {'live': 0, 'pending': 1, 'draft': 2}
    # The page's order: published, then needing attention, then the rest.
    events = sorted(events, key=lambda e: (rank.get(e.status, 3), str(e.name)))
    c.check_size(len(events))
    return event_sheets(events), len(events)


# =====================================================================
# Payouts
# =====================================================================

def payouts_filters(params):
    return {'status': c.choice(params, 'status', ('All', 'pending', 'completed', 'failed'), 'All')}


@report('payouts', 'admin_payouts', 'payouts', 'payouts', payouts_filters)
def payouts_report(f):
    payouts = Payout.objects.select_related('vendor_user').order_by('-period_start', 'vendor', 'id')
    if f['status'] != 'All':
        payouts = payouts.filter(status=f['status'])
    payouts = list(payouts)
    c.check_size(len(payouts))
    columns = [
        col('Payout ID', 10, INT),
        col('Vendor', 24),
        col('Vendor Phone', 14),
        col('Period', 16),
        col('Period Start', 13, DATE),
        col('Period End', 13, DATE),
        col('Payout Amount', 14, MONEY),
        col('Status', 11),
        col('Generated On', 18, DATETIME),
    ]
    rows = [[
        p.id, p.vendor, p.vendor_user.phone if p.vendor_user_id else '', p.period,
        p.period_start, p.period_end, p.gross, str(p.status).capitalize(), p.created_at,
    ] for p in payouts]
    return [Sheet('Payouts', columns, rows)], len(payouts)


# =====================================================================
# Refunds
# =====================================================================

def refunds_filters(params):
    return {'tab': c.choice(params, 'tab', ('requests', 'paused', 'history'), 'requests')}


@report('refunds', 'admin_refunds', 'refunds', 'bookings', refunds_filters)
def refunds_report(f):
    bookings = Booking.objects.exclude(status='removed').select_related('listing__vendor')
    tab = f['tab']
    if tab == 'requests':
        selected = list(bookings.filter(status='refund_pending'))
    elif tab == 'history':
        selected = list(bookings.filter(status='refunded'))
    else:
        # The page's "Paused venues" tab: still-upcoming bookings paid ONLINE
        # at a venue the admin paused (matched by name, base or unit sibling).
        paused = [
            str(l.name or (l.record or {}).get('name') or '')
            for l in Listing.objects.select_related('vendor')
            if not _unit_of(l) and _venue_status(l) == 'paused'
        ]

        def on_paused(name):
            name = str(name or '')
            return any(name == p or name.startswith(p + ' — ') for p in paused)

        selected = [
            b for b in bookings.filter(status='confirmed').exclude(
                method__in=('venue', 'walk-in', 'cash')
            )
            if b.display_status == 'confirmed' and on_paused(b.venue_name)
        ]
    c.check_size(len(selected))

    state = {'refund_pending': 'Refund requested', 'refunded': 'Refunded',
             'confirmed': 'Awaiting refund (venue paused)'}
    columns = [
        col('Booking ID', 16),
        col('Venue', 28),
        col('Vendor', 20),
        col('Customer Name', 22),
        col('Customer Phone', 14),
        col('Booking Date', 13, DATE),
        col('Time Slots', 28),
        col('Payment Method', 14),
        col('Booking Amount', 14, MONEY),
        col('Captured Online', 14, MONEY),
        col('Refund Status', 26),
        col('Refund Amount', 13, MONEY),
        col('Refund Amount Basis', 30),
        col('Refund Reason', 30),
        col('Refund ID', 22),
        col('Razorpay Payment ID', 22),
        col('Booked On', 18, DATETIME),
    ]

    def rows():
        for b in sorted(selected, key=lambda b: b.created_at, reverse=True):
            captured = c.venue_captured(b)
            amount, basis = c.refund_figures(b.status, b.refund_amount, captured)
            vendor = b.listing.vendor if b.listing_id else None
            yield [
                b.id, b.venue_name, (vendor.name or vendor.phone) if vendor else '',
                b.customer_name, b.phone, b.date, ', '.join(b.slots or []),
                c.method_label(b.method), b.amount, captured,
                state.get(b.status, b.status), amount, basis, b.refund_reason,
                b.refund_id, b.razorpay_payment_id, b.created_at,
            ]

    return [Sheet('Refunds', columns, rows())], len(selected)


# =====================================================================
# Payments report (reconciliation)
# =====================================================================

def payments_filters(params):
    start, end = c.date_range(params)
    return {'from': start, 'to': end}


def _venue_status_key(b):
    status = b.status or 'confirmed'
    if status == 'payment_pending' and b.hold_expired:
        return 'expired'
    return status


def _event_status_key(o):
    if o.status == 'payment_pending' and o.hold_expired:
        return 'expired'
    return o.status


@report('payments', 'admin_payments', 'payments report', 'records', payments_filters)
def payments_report(f):
    venue = list(c.created_between(
        Booking.objects.exclude(status='removed'), f['from'], f['to'],
    ).select_related('listing__vendor').order_by('-created_at'))
    event = list(c.created_between(
        EventOrder.objects.exclude(status='removed'), f['from'], f['to'],
    ).select_related('event__vendor').order_by('-created_at'))
    c.check_size(len(venue))
    c.check_size(len(event))

    totals = {k: [0, 0] for k in (
        'records', 'kept', 'gross', 'fees', 'promo', 'cash', 'captured',
        'refunded', 'refund_pending', 'cancelled_paid', 'unpaid',
    )}

    def add(key, index, value):
        totals[key][index] += value or 0

    venue_rows, event_rows, refund_rows = [], [], []

    for b in venue:
        captured = c.venue_captured(b)
        kept = c.is_kept(b.status)
        refunded, basis = c.refund_figures(b.status, b.refund_amount, captured)
        cash = c.venue_cash_due(b) if kept else 0
        add('records', 0, 1)
        if kept:
            add('kept', 0, 1)
            add('gross', 0, b.amount)
            add('fees', 0, 0 if c.is_walk_in(b) else b.fee)
            add('promo', 0, c.promo_top_up(b))
            add('cash', 0, cash)
        add('captured', 0, captured)
        add('refunded', 0, refunded)
        if b.status == 'refund_pending':
            add('refund_pending', 0, captured)
        cancelled_paid = b.status == 'cancelled' and bool(b.razorpay_payment_id)
        if cancelled_paid:
            add('cancelled_paid', 0, captured)
        if b.status in ('payment_pending', 'failed') or (
            b.status == 'cancelled' and not b.razorpay_payment_id
        ):
            add('unpaid', 0, 1)
        vendor = b.listing.vendor if b.listing_id else None
        key = _venue_status_key(b)
        refund_state = c.REFUND_STATUS_LABEL.get(b.status, '')
        if cancelled_paid:
            refund_state = 'Cancelled after payment — no refund recorded'
        venue_rows.append([
            b.id, b.created_at, b.venue_name,
            (vendor.name or vendor.phone) if vendor else '',
            b.customer_name, b.phone, b.date, c.method_label(b.method),
            c.VENUE_STATUS_LABEL.get(key, key), kept, b.amount,
            0 if c.is_walk_in(b) else b.fee, b.discount_amount or None,
            c.offer_funding(b), captured, cash or None, refund_state, refunded, basis,
            b.refund_reason, b.razorpay_order_id, b.razorpay_payment_id, b.refund_id,
        ])
        if refund_state:
            refund_rows.append([
                'Venue booking', b.id, b.created_at, b.venue_name, b.customer_name,
                b.phone, captured, refund_state, refunded, basis, b.refund_reason,
                b.razorpay_payment_id, b.refund_id,
            ])

    for o in event:
        captured = o.amount if o.razorpay_payment_id else 0
        kept = o.status == 'confirmed'
        refunded, basis = c.refund_figures(o.status, o.refund_amount, captured)
        add('records', 1, 1)
        if kept:
            add('kept', 1, 1)
            add('gross', 1, o.amount)
            add('fees', 1, o.fee)
        add('captured', 1, captured)
        add('refunded', 1, refunded)
        if o.status == 'refund_pending':
            add('refund_pending', 1, captured)
        cancelled_paid = o.status == 'cancelled' and bool(o.razorpay_payment_id)
        if cancelled_paid:
            add('cancelled_paid', 1, captured)
        if o.status in ('payment_pending', 'failed') or (
            o.status == 'cancelled' and not o.razorpay_payment_id
        ):
            add('unpaid', 1, 1)
        organiser = ''
        if o.event_id and o.event:
            organiser = o.event.organizer
        key = _event_status_key(o)
        refund_state = c.REFUND_STATUS_LABEL.get(o.status, '')
        if cancelled_paid:
            refund_state = 'Cancelled after payment — no refund recorded'
        event_rows.append([
            o.id, o.created_at, o.event_name, organiser, o.customer_name, o.phone,
            o.date, c.tickets_text(o.items), o.ticket_count, o.subtotal, o.fee,
            o.amount, c.EVENT_STATUS_LABEL.get(key, key), kept, captured,
            refund_state, refunded, basis, o.refund_reason, o.razorpay_order_id,
            o.razorpay_payment_id, o.refund_id,
        ])
        if refund_state:
            refund_rows.append([
                'Event ticket', o.id, o.created_at, o.event_name, o.customer_name,
                o.phone, captured, refund_state, refunded, basis, o.refund_reason,
                o.razorpay_payment_id, o.refund_id,
            ])

    refund_rows.sort(key=lambda r: r[2], reverse=True)

    if f['from'] or f['to']:
        period = (
            f"{f['from'].strftime('%d %b %Y') if f['from'] else 'the first booking'} to "
            f"{f['to'].strftime('%d %b %Y') if f['to'] else 'today'}"
            ' (day the booking was made, IST)'
        )
    else:
        period = 'All time'

    def line(label, key, fmt, note):
        v, e = totals[key]
        return [label, Cell(v, fmt), Cell(e, fmt), Cell(v + e, fmt, bold=True), note]

    net = [totals['captured'][i] - totals['refunded'][i] for i in (0, 1)]
    summary = [
        ['Report period', None, None, None, period],
        ['Generated', None, None, None, timezone.now()],
        line('Records in period', 'records', INT,
             'Every booking / ticket order made in the period, any status (admin-removed rows excluded).'),
        line('Kept bookings', 'kept', INT,
             'Bookings that still stand: not unpaid, failed, cancelled, refund-pending or refunded.'),
        line('Gross booking value (kept)', 'gross', MONEY,
             'Booking totals, platform fee included, of kept bookings only. Unpaid, cancelled and refunded bookings are NOT counted.'),
        line('Platform fees (kept)', 'fees', MONEY,
             "The platform's revenue: the booking-time fee frozen on each kept booking. Walk-ins carry none."),
        line('Platform-funded promo discounts (kept)', 'promo', MONEY,
             'Discounts on platform offers, paid back to the vendor. Venue-funded offers are not included.'),
        line('Collected at venue (kept)', 'cash', MONEY,
             'Paid in person, never through the platform: walk-ins, legacy pay-at-venue, and the venue share of part payments.'),
        line('Captured online (payment gateway)', 'captured', MONEY,
             'Every payment the gateway recorded (rows with a Razorpay payment id), whatever happened to the booking later.'),
        line('Refunds issued', 'refunded', MONEY,
             'Recorded refund amounts; where a refund was issued without a partial amount, the full captured amount (see Refund Amount Basis).'),
        ['Net captured after refunds issued', Cell(net[0], MONEY), Cell(net[1], MONEY),
         Cell(net[0] + net[1], MONEY, bold=True), 'Captured online minus refunds issued.'],
        line('Refunds pending', 'refund_pending', MONEY,
             'Captured amounts on bookings waiting for a refund — still held, owed back.'),
        line('Paid then cancelled — no refund recorded', 'cancelled_paid', MONEY,
             'Captured payments on cancelled bookings with no refund on record. Check these against the payment gateway.'),
        line('Unpaid / failed / abandoned attempts', 'unpaid', INT,
             'Checkouts that never captured money. They carry no revenue.'),
    ]
    sheets = [
        Sheet('Summary', [
            col('Metric', 40), col('Venue Bookings', 16), col('Event Tickets', 16),
            col('Total', 16), col('Notes', 90),
        ], summary, filters=False),
        Sheet('Venue Payments', [
            col('Booking ID', 16), col('Booked On', 18, DATETIME), col('Venue', 28),
            col('Vendor', 20), col('Customer Name', 22), col('Customer Phone', 14),
            col('Booking Date', 13, DATE), col('Payment Method', 14),
            col('Booking Status', 18), col('Kept', 7), col('Booking Amount', 14, MONEY),
            col('Platform Fee', 12, MONEY), col('Discount', 11, MONEY),
            col('Discount Funded By', 12), col('Captured Online', 14, MONEY),
            col('Collected at Venue', 14, MONEY), col('Refund Status', 26),
            col('Refund Amount', 13, MONEY), col('Refund Amount Basis', 30),
            col('Refund Reason', 28), col('Razorpay Order ID', 22),
            col('Razorpay Payment ID', 22), col('Refund ID', 22),
        ], venue_rows),
        Sheet('Event Payments', [
            col('Order ID', 16), col('Booked On', 18, DATETIME), col('Event', 28),
            col('Organiser', 20), col('Customer Name', 22), col('Customer Phone', 14),
            col('Event Date', 13, DATE), col('Tickets', 26), col('Ticket Count', 9, INT),
            col('Ticket Subtotal', 13, MONEY), col('Platform Fee', 12, MONEY),
            col('Order Amount', 13, MONEY), col('Order Status', 18), col('Kept', 7),
            col('Captured Online', 14, MONEY), col('Refund Status', 26),
            col('Refund Amount', 13, MONEY), col('Refund Amount Basis', 30),
            col('Refund Reason', 28), col('Razorpay Order ID', 22),
            col('Razorpay Payment ID', 22), col('Refund ID', 22),
        ], event_rows),
        Sheet('Refunds', [
            col('Type', 14), col('Booking ID', 16), col('Booked On', 18, DATETIME),
            col('Venue / Event', 28), col('Customer Name', 22), col('Customer Phone', 14),
            col('Captured Online', 14, MONEY), col('Refund Status', 30),
            col('Refund Amount', 13, MONEY), col('Refund Amount Basis', 30),
            col('Refund Reason', 28), col('Razorpay Payment ID', 22), col('Refund ID', 22),
        ], refund_rows),
    ]
    return sheets, len(venue) + len(event)


# =====================================================================
# Audit log
# =====================================================================

def audit_filters(params):
    return {}


@report('audit', 'admin_audit_log', 'audit log', 'entries', audit_filters)
def audit_report(f):
    entries = AuditEntry.objects.order_by('-created_at', '-id')
    count = entries.count()
    c.check_size(count)
    columns = [
        col('When', 18, DATETIME), col('Admin', 24), col('Action', 28),
        col('Target', 30), col('Target ID', 38), col('Change', 60),
    ]
    rows = (
        [e.created_at, e.admin, e.action, e.target, e.target_id, e.change]
        for e in entries.iterator(chunk_size=2000)
    )
    return [Sheet('Audit Log', columns, rows)], count
