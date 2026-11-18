"""
The server's price for a booking draft, before any money moves.

    POST /api/bookings/quote   body: the booking draft, as for POST
                               /api/payments/order (venueId, date, slots,
                               sport/unit, addons, offer, payFull…)

The customer's review step shows THIS bill, so the total on screen is the
total the payment order will charge — whichever way the customer reached it
(package first or time first, weekend or weekday, a pitch's own listing or
the base venue). It runs the same validate_booking_request the hold and the
payment order run (QA audit 2026-11-02: "the same booking configuration
must produce the same total regardless of the booking path"); the client's
own amount is neither required nor trusted.

Read only: nothing is reserved and no row is written. Availability is still
decided by the hold (POST /api/bookings/holds) and re-checked under the
listing lock when the order is created.
"""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .views import validate_booking_request


def quote_payload(data):
    breakdown = data.get('breakdown') or {}
    return {
        'amount': data['amount'],
        'fee': data['fee'],
        'discount': data['discount'],
        'subtotal': breakdown.get('subtotal'),
        'slotCharge': breakdown.get('slotCharge'),
        'rate': breakdown.get('rate'),
        'minutes': breakdown.get('minutes'),
        'isWeekend': breakdown.get('isWeekend', False),
        'package': breakdown.get('package'),
        'lines': breakdown.get('lines') or [],
        'offer': data.get('applied_offer'),
        'payNow': data['pay_now'],
        'atVenue': data['at_venue'],
        # The time actually reserved (slot labels as stored).
        'slots': data['slots'],
    }


class QuoteView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        data, error = validate_booking_request(body, user=request.user, quote=True)
        if error is not None:
            return error
        return Response({'quote': quote_payload(data)})
