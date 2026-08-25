"""
Security regression tests — every test here corresponds to a real exploit
found in the full security review, reproduced BEFORE it was fixed.

These are the ones that cost money if they ever come back, so each test
states the attack in plain terms rather than just asserting a status code.
"""
import datetime
import uuid

from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from venues.models import Listing

from .models import Booking
from .slots import today_ist
from .tests import PLAYZONE_RECORD, RECORD, TOMORROW


class FreeOnlineBookingTests(APITestCase):
    """
    A customer must not be able to mark a booking 'paid' without going through
    Razorpay. The direct endpoint used to accept method='upi'/'online'/'card'/
    'netbanking' and create a CONFIRMED booking with no payment — which
    adminpanel/payouts.py then paid out to the vendor in real money.

    Pay-at-venue has since been retired too, so this endpoint now creates
    nothing at all: there is no path to a confirmed booking that does not go
    through the gateway.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9000000002', name='Asha')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='hall',
            record={**RECORD, 'id': 'x', 'status': 'live'},
            name='Grand Palace Hall', category='hall',
            locality='Indiranagar', pincode='560038',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, **overrides):
        return self.client.post('/api/users/me/bookings', {
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [],
            'amount': 920, 'perSlot': 600, **overrides,
        }, format='json')

    def test_no_method_can_create_a_booking_without_paying(self):
        """The whole exploit: claim you paid, pay nothing, get confirmed."""
        for method in ('online', 'upi', 'card', 'netbanking', 'venue', None):
            body = {} if method is None else {'method': method}
            response = self.book(**body)
            self.assertEqual(
                response.status_code, 400,
                f'{method} was accepted without any payment',
            )
            self.assertEqual(response.data['code'], 'PAYMENT_REQUIRED')
        self.assertEqual(
            Booking.objects.count(), 0,
            'the retired endpoint created a booking',
        )


class GatewayBookingMixin:
    """Book through the payment gateway (the only create path there is now).

    Without this, these tests would post to the retired direct endpoint and
    get a 400 about payment — passing for entirely the wrong reason.
    """

    _seq = 0

    def gateway_book(self, body):
        from unittest.mock import MagicMock, patch

        GatewayBookingMixin._seq += 1
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': f'order_SEC{GatewayBookingMixin._seq}'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            return self.client.post('/api/payments/order', body, format='json')


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class UnitPricingIntegrityTests(GatewayBookingMixin, APITestCase):
    """
    A bogus `sport` used to fall through to the venue's top-level price
    (often absent -> ₹0) AND get a different conflict key, so the same
    physical pitch could be sold twice at near-zero cost.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000003', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9000000004', name='Ravi')
        self.turf = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='turf',
            record={**PLAYZONE_RECORD, 'id': 'y', 'status': 'live'},
            name='Turf Arena', category='Box cricket',
            locality='HSR', pincode='560102',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, **overrides):
        return self.gateway_book({
            'venueId': str(self.turf.id), 'date': TOMORROW,
            'slots': ['19:00 – 21:00'], 'sport': 'Box Cricket', 'unit': 1,
            'perSlot': 599, 'addons': [], 'amount': 1218, **overrides,
        })

    def test_unknown_sport_is_rejected_not_priced_at_base(self):
        response = self.book(sport='does-not-exist', perSlot=0, amount=20)
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.data.get('code'), 'AMOUNT_MISMATCH')

    def test_unit_beyond_declared_count_is_rejected(self):
        """Pitch 99 does not exist — booking it must not silently succeed."""
        response = self.book(unit=99, perSlot=0, amount=20)
        self.assertEqual(response.status_code, 400)

    def test_bogus_sport_cannot_double_sell_a_real_pitch(self):
        self.assertEqual(self.book().status_code, 201)
        # Same pitch, same slot, but a made-up sport name to dodge the
        # conflict key. Must not create a second booking.
        clash = self.book(sport='anything-else', perSlot=0, amount=20)
        self.assertEqual(clash.status_code, 400)
        self.assertEqual(Booking.objects.count(), 1)


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class NegativePriceTests(GatewayBookingMixin, APITestCase):
    """
    Vendor-entered prices are stored verbatim. A negative price (typo or
    malice) used to drive the booking total to ₹0 for every customer,
    bypassing the coupon system entirely.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000005', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9000000006', name='Sam')
        record = {
            **RECORD, 'id': 'z', 'status': 'live',
            'detail': {**RECORD['detail'],
                       'addons': [{'name': 'Discount hack', 'price': -100000}]},
        }
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='neg',
            record=record, name='Neg Hall', category='hall',
            locality='X', pincode='560001',
        )
        self.client.force_authenticate(user=self.customer)

    def test_negative_addon_price_cannot_zero_the_bill(self):
        response = self.gateway_book({
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'perSlot': 600,
            'addons': [{'name': 'Discount hack', 'qty': 1}], 'amount': 20,
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Booking.objects.count(), 0)


class AvailabilityDoSTests(APITestCase):
    """
    `detail.sports[].units` is vendor-supplied and was used directly as a
    loop bound on the PUBLIC, uncached availability endpoint.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000007', name='Vendor', role=User.Role.VENDOR,
        )
        record = {
            **PLAYZONE_RECORD, 'id': 'w', 'status': 'live',
            'detail': {'sports': [
                {'name': 'Box Cricket', 'price': '599', 'units': 10 ** 12},
            ]},
        }
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='dos',
            record=record, name='DoS Turf', category='Box cricket',
            locality='X', pincode='560001',
        )

    def test_absurd_unit_count_does_not_hang_the_endpoint(self):
        """Must return promptly instead of looping a trillion times."""
        response = self.client.get(
            f'/api/venues/{self.listing.id}/availability?date={TOMORROW}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(response.data.get('unitRates') or []), 64)
