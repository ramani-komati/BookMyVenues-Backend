"""
Per-venue part payment: pay a slice online, the rest in cash at the venue.

The split is duplicated in the customer app. If the two ever disagree, every
part-paid booking dies on the amount check — so the formula is pinned here
value-for-value.
"""
import datetime
import uuid

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from venues.models import Listing

from .models import Booking
from .part_payment import read_config, split
from .test_security import GatewayBookingMixin
from .tests import RECORD, TOMORROW


def cfg(mode, value, enabled=True):
    return {'partPayment': {'enabled': enabled, 'mode': mode, 'value': value}}


class SplitMathTests(SimpleTestCase):
    """raw = fixed ? min(value,total) : round(total*value/100)
       payNow = min(total, max(raw, min(fee,total)));  atVenue = total-payNow"""

    FEE = 20

    def at(self, record, total):
        return split(total, read_config(record), self.FEE)

    def test_percent(self):
        self.assertEqual(self.at(cfg('percent', 20), 1000), (200, 800))
        self.assertEqual(self.at(cfg('percent', 50), 1000), (500, 500))
        self.assertEqual(self.at(cfg('percent', 99), 1000), (990, 10))

    def test_fixed(self):
        self.assertEqual(self.at(cfg('fixed', 200), 1000), (200, 800))
        # A fixed slice larger than the booking cannot exceed it.
        self.assertEqual(self.at(cfg('fixed', 5000), 1000), (1000, 0))

    def test_the_fee_is_always_covered_online(self):
        """1% of ₹100 is ₹1 — but our ₹20 fee has to come out of the online
        slice, so payNow is floored at the fee."""
        self.assertEqual(self.at(cfg('percent', 1), 100), (20, 80))

    def test_a_total_below_the_fee_is_charged_in_full(self):
        self.assertEqual(self.at(cfg('percent', 50), 10), (10, 0))

    def test_rounding_matches_the_frontend(self):
        # round(333 * 20 / 100) = round(66.6) = 67
        self.assertEqual(self.at(cfg('percent', 20), 333), (67, 266))

    def test_disabled_or_absent_pays_in_full(self):
        self.assertEqual(self.at(cfg('percent', 20, enabled=False), 1000), (1000, 0))
        self.assertEqual(self.at({}, 1000), (1000, 0))

    def test_a_malformed_config_fails_safe_to_full_payment(self):
        """Never guess — an unusable config charges the full amount, which is
        the safe direction to be wrong in."""
        for bad in ({'partPayment': {'enabled': True, 'mode': 'half', 'value': 20}},
                    {'partPayment': {'enabled': True, 'mode': 'percent', 'value': 0}},
                    {'partPayment': {'enabled': True, 'mode': 'percent', 'value': 150}},
                    {'partPayment': {'enabled': True, 'mode': 'percent', 'value': 'x'}},
                    {'partPayment': {'enabled': True}},
                    {'partPayment': 'yes'}):
            self.assertIsNone(read_config(bad), bad)
            self.assertEqual(split(1000, read_config(bad), 20), (1000, 0))

    def test_the_two_halves_always_reconstruct_the_total(self):
        for total in (1, 19, 20, 21, 100, 333, 999, 12345):
            for config in (cfg('percent', 1), cfg('percent', 50),
                           cfg('percent', 99), cfg('fixed', 200)):
                pay_now, at_venue = split(total, read_config(config), 20)
                self.assertEqual(pay_now + at_venue, total)
                self.assertGreaterEqual(pay_now, min(20, total))
                self.assertGreaterEqual(at_venue, 0)


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class PartPaymentBookingTests(GatewayBookingMixin, APITestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9800000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9800000002', name='Asha')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='pp-hall',
            record={**RECORD, 'id': 'pp', 'status': 'live', 'price': 600,
                    'partPayment': {'enabled': True, 'mode': 'percent', 'value': 20}},
            name='Part Pay Hall', category='hall', locality='X', pincode='560001',
        )
        self.full = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='full-hall',
            record={**RECORD, 'id': 'full', 'status': 'live', 'price': 600},
            name='Full Pay Hall', category='hall', locality='X', pincode='560002',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, listing=None, amount=920):
        return self.gateway_book({
            'venueId': str((listing or self.listing).id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [], 'perSlot': 600,
            'amount': amount,
        })

    def test_the_order_charges_only_the_online_slice(self):
        r = self.book()
        self.assertEqual(r.status_code, 201, r.data)
        # 1.5h x 600 = 900 + 20 fee = 920 total; 20% = 184.
        self.assertEqual(r.data['amount'], 184 * 100)   # paise, to the widget
        self.assertEqual(r.data['payNow'], 184)
        self.assertEqual(r.data['atVenue'], 736)

    def test_the_booking_keeps_the_FULL_amount(self):
        r = self.book()
        booking = Booking.objects.get(pk=r.data['bookingId'])
        self.assertEqual(booking.amount, 920)        # unchanged
        self.assertEqual(booking.pay_now, 184)
        self.assertEqual(booking.at_venue, 736)
        self.assertEqual(booking.part_payment,
                         {'enabled': True, 'mode': 'percent', 'value': 20})

    def test_the_client_still_sends_the_full_total(self):
        """The amount check is on the TOTAL — sending payNow must not pass."""
        r = self.book(amount=184)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(r.data['expectedAmount'], 920)

    def test_the_record_echoes_the_split(self):
        r = self.book()
        Booking.objects.update(status='confirmed')
        mine = self.client.get('/api/users/me/bookings').data['bookings'][0]
        self.assertEqual(mine['amount'], 920)
        self.assertEqual(mine['payNow'], 184)
        self.assertEqual(mine['atVenue'], 736)
        self.assertEqual(mine['partPayment']['value'], 20)

    def test_a_venue_without_part_payment_is_unchanged(self):
        r = self.book(listing=self.full)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['payNow'], 920)
        self.assertEqual(r.data['atVenue'], 0)
        booking = Booking.objects.get(pk=r.data['bookingId'])
        self.assertEqual(booking.pay_now, 920)
        self.assertEqual(booking.at_venue, 0)
        self.assertIsNone(booking.part_payment)

    def test_a_legacy_booking_reads_as_fully_paid_online(self):
        """pay_now is NULL for every booking taken before this feature."""
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, venue_name='X',
            category='hall', location='x', image='', customer_name='Asha',
            phone='9800000002', date=datetime.date.fromisoformat(TOMORROW),
            slots=['08:00 – 09:00'],
            per_slot=600, addons=[], fee=20, amount=620, method='upi',
            status='confirmed',
        )
        self.assertIsNone(booking.pay_now)
        self.assertEqual(booking.online_amount, 620)
        self.assertEqual(booking.as_record()['payNow'], 620)
        self.assertEqual(booking.as_record()['atVenue'], 0)
