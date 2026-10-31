"""
GET /api/admin/data-review — records that LOOK like test data (QA audit
2026-10-30: "Durga Play Ground Demo Venue" at ₹1/hour, an event named
"bookmyvenue", event orders against a venue called "ku").

READ-ONLY. Nothing here changes or deletes anything: it lists candidates and
why each was flagged, so the super admin can confirm and then use the normal,
audited actions — pause a venue (PATCH venues/<id> {status: paused}) or
unpublish an event (PATCH events/<id> {status: unpublish}). Both are
reversible and keep every booking, ticket, payment and invoice. Orders are
listed for information only and are never altered — they are customer and
payment history.

`python manage.py review_test_data` prints the same report.
"""
import re

from rest_framework.response import Response
from rest_framework.views import APIView

from .auth import CsrfExemptSessionAuthentication, IsAdmin

TEST_WORDS = re.compile(r'\b(demo|test|testing|dummy|sample|trial|fake)\b', re.I)
BRAND_NAMES = {'bookmyvenue', 'bookmyvenues', 'thebookmyvenue', 'thebookmyvenues'}
SUSPICIOUS_PRICE = 10   # ₹ — no real venue rents for less per slot / hour


def _price(value):
    match = re.search(r'\d+(?:\.\d+)?', str(value or ''))
    return float(match.group()) if match else None


def name_reasons(name):
    text = str(name or '').strip()
    reasons = []
    if TEST_WORDS.search(text):
        reasons.append(f'Name contains "{TEST_WORDS.search(text).group(1)}"')
    if re.sub(r'[^a-z]', '', text.lower()) in BRAND_NAMES:
        reasons.append('Named after the platform itself')
    if 0 < len(re.sub(r'\W', '', text)) <= 2:
        reasons.append('Name is only one or two characters')
    return reasons


def venue_candidates():
    from venues.models import Listing
    rows = []
    for listing in Listing.objects.exclude(status=Listing.Status.DELETED).order_by('name'):
        reasons = name_reasons(listing.name)
        price = _price((listing.record or {}).get('price'))
        if price is not None and 0 < price <= SUSPICIOUS_PRICE:
            reasons.append(f'Price is ₹{price:g}')
        if reasons:
            rows.append({
                'id': str(listing.pk), 'name': listing.name, 'status': listing.status,
                'price': (listing.record or {}).get('price'), 'unit': (listing.record or {}).get('unit'),
                'bookings': listing.bookings.count(),
                'reasons': reasons,
            })
    return rows


def event_candidates():
    from events.models import Event
    rows = []
    for event in Event.objects.exclude(status=Event.Status.DELETED).order_by('name'):
        reasons = name_reasons(event.name) + [f'Venue {r[0].lower()}{r[1:]}' for r in name_reasons(event.venue_name)]
        if reasons:
            rows.append({
                'id': str(event.pk), 'name': event.name, 'venueName': event.venue_name,
                'status': event.status, 'orders': event.orders.count(),
                'reasons': reasons,
            })
    return rows


def order_candidates(flagged_event_ids):
    from events.models import EventOrder
    rows = []
    for order in EventOrder.objects.select_related('event').order_by('-created_at')[:2000]:
        reasons = []
        if str(order.event_id) in flagged_event_ids:
            reasons.append('Order for a flagged event')
        venue = getattr(order.event, 'venue_name', '') if order.event_id else ''
        reasons += [f'Venue {r[0].lower()}{r[1:]}' for r in name_reasons(venue)]
        if reasons:
            rows.append({
                'id': order.pk, 'eventName': order.event_name, 'venueName': venue,
                'status': order.status, 'amount': order.amount,
                'paid': bool(order.razorpay_payment_id),
                'createdAt': order.created_at.isoformat() if order.created_at else None,
                'reasons': sorted(set(reasons)),
            })
    return rows


def report():
    venues = venue_candidates()
    events = event_candidates()
    orders = order_candidates({e['id'] for e in events})
    return {'venues': venues, 'events': events, 'orders': orders}


class AdminDataReviewView(APIView):
    """GET /api/admin/data-review — flagged candidates only, never an action."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(report())
