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


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class PartPaymentInheritanceTests(GatewayBookingMixin, APITestCase):
    """
    REGRESSION: part payment lives on the parent venue, but a theatre screen
    or turf court is booked through its own sibling listing — which has no
    config, so the order charged the FULL amount while the bill said "Pay
    ₹100 now".
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9850000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9850000002', name='Asha')
        self.base = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='og-theatre',
            record={**RECORD, 'id': 'base', 'status': 'live', 'price': 600,
                    'partPayment': {'enabled': True, 'mode': 'fixed', 'value': 100}},
            name='OG Theatre', category='Private theatre',
            locality='X', pincode='560001', status=Listing.Status.LIVE,
        )
        self.screen = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='og-theatre-screen-2',
            record={**RECORD, 'id': 'screen', 'status': 'live', 'price': 600,
                    'detail': {**RECORD['detail'], 'unitOf': str(self.base.id)}},
            name='OG Theatre — Screen 2', category='Private theatre',
            locality='X', pincode='560001', status=Listing.Status.LIVE,
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, listing):
        return self.gateway_book({
            'venueId': str(listing.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [], 'perSlot': 600,
            'amount': 920,
        })

    def test_booking_a_screen_charges_only_the_parents_slice(self):
        """The bug: this charged ₹920 instead of ₹100."""
        r = self.book(self.screen)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['payNow'], 100)
        self.assertEqual(r.data['atVenue'], 820)
        self.assertEqual(r.data['amount'], 100 * 100)   # paise to the widget

    def test_the_screen_booking_records_the_split(self):
        r = self.book(self.screen)
        booking = Booking.objects.get(pk=r.data['bookingId'])
        self.assertEqual(booking.amount, 920)           # full total, unchanged
        self.assertEqual(booking.pay_now, 100)
        self.assertEqual(booking.at_venue, 820)
        self.assertEqual(booking.part_payment['value'], 100)

    def test_the_parent_venue_still_works(self):
        r = self.book(self.base)
        self.assertEqual(r.data['payNow'], 100)

    def test_the_public_detail_of_a_screen_quotes_the_same_split(self):
        """The bill and the gateway must agree — that mismatch was the bug."""
        data = self.client.get(f'/api/venues/{self.screen.id}').data
        self.assertEqual(data['detail']['partPayment']['value'], 100)
        self.assertEqual(data['partPayment']['mode'], 'fixed')

    def test_a_screen_with_its_OWN_config_is_not_overridden(self):
        self.screen.record = {
            **self.screen.record,
            'partPayment': {'enabled': True, 'mode': 'percent', 'value': 50},
        }
        self.screen.save(update_fields=['record'])
        r = self.book(self.screen)
        self.assertEqual(r.data['payNow'], 460)        # 50% of 920, not ₹100

    def test_a_screen_whose_parent_has_none_pays_in_full(self):
        self.base.record = {k: v for k, v in self.base.record.items()
                            if k != 'partPayment'}
        self.base.save(update_fields=['record'])
        r = self.book(self.screen)
        self.assertEqual(r.data['payNow'], 920)
        self.assertEqual(r.data['atVenue'], 0)

    def test_a_broken_unitOf_does_not_crash_the_booking(self):
        self.screen.record = {
            **self.screen.record,
            'detail': {**self.screen.record['detail'], 'unitOf': 'not-a-uuid'},
        }
        self.screen.save(update_fields=['record'])
        r = self.book(self.screen)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['payNow'], 920)        # falls back to full


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class PayFullOptOutTests(GatewayBookingMixin, APITestCase):
    """
    A customer at a part-payment venue may choose to settle the whole bill
    online. Unlike payNow, this flag is trusted — it can only move the charge
    UP to the full total, so tampering can do nothing worse than make someone
    overpay their own booking.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9860000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9860000002', name='Asha')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='pf-hall',
            record={**RECORD, 'id': 'pf', 'status': 'live', 'price': 600,
                    'partPayment': {'enabled': True, 'mode': 'percent',
                                    'value': 20}},
            name='Pay Full Hall', category='hall',
            locality='X', pincode='560001', status=Listing.Status.LIVE,
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, **extra):
        return self.gateway_book({
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [], 'perSlot': 600,
            'amount': 920, **extra,
        })

    def test_pay_full_charges_the_whole_bill(self):
        r = self.book(payFull=True)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['payNow'], 920)
        self.assertEqual(r.data['atVenue'], 0)
        self.assertEqual(r.data['amount'], 920 * 100)     # paise to the widget

    def test_the_booking_records_no_split(self):
        """Paid in full IS no split — the record should not claim otherwise."""
        r = self.book(payFull=True)
        booking = Booking.objects.get(pk=r.data['bookingId'])
        self.assertEqual(booking.amount, 920)
        self.assertEqual(booking.pay_now, 920)
        self.assertEqual(booking.at_venue, 0)
        self.assertIsNone(booking.part_payment)

    def test_without_the_flag_the_split_still_applies(self):
        r = self.book()
        self.assertEqual(r.data['payNow'], 184)           # 20% of 920
        self.assertEqual(r.data['atVenue'], 736)

    # Each case needs its own slot, or the second booking hits the overlap
    # guard and we would be asserting against a 409 instead of an order.
    SLOTS = ['08:00 – 09:30', '10:00 – 11:30', '12:00 – 13:30',
             '14:00 – 15:30', '16:00 – 17:30', '18:00 – 19:30',
             '20:00 – 21:30', '22:00 – 23:30']

    def test_pay_full_false_keeps_the_split(self):
        for i, falsey in enumerate((False, 'false', 'False', 0, '0', '', None, 'no')):
            r = self.book(payFull=falsey, slots=[self.SLOTS[i]])
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data['payNow'], 184, f'payFull={falsey!r}')

    def test_the_string_true_is_accepted(self):
        """JSON from some clients sends booleans as strings."""
        for i, truthy in enumerate((True, 'true', 'True', 1, '1', 'yes')):
            r = self.book(payFull=truthy, slots=[self.SLOTS[i]])
            self.assertEqual(r.status_code, 201, r.data)
            self.assertEqual(r.data['payNow'], 920, f'payFull={truthy!r}')

    def test_the_amount_is_still_recomputed(self):
        """payFull does not let a client name its own total."""
        r = self.book(payFull=True, amount=1)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(r.data['expectedAmount'], 920)

    def test_pay_full_on_a_venue_without_part_payment_is_a_no_op(self):
        plain = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='plain-pf',
            record={**RECORD, 'id': 'plain', 'status': 'live', 'price': 600},
            name='Plain', category='hall', locality='X', pincode='560002',
            status=Listing.Status.LIVE,
        )
        r = self.gateway_book({
            'venueId': str(plain.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [], 'perSlot': 600,
            'amount': 920, 'payFull': True,
        })
        self.assertEqual(r.data['payNow'], 920)
        self.assertEqual(r.data['atVenue'], 0)

    def test_the_vendor_is_owed_the_same_either_way(self):
        """Payout + cash must reconcile to amount - fee on both paths."""
        from bookings.vendor_views import booking_net

        split_booking = Booking.objects.get(pk=self.book().data['bookingId'])
        full_booking = Booking.objects.get(
            pk=self.book(payFull=True, slots=['21:00 – 22:30']).data['bookingId']
        )
        for booking in (split_booking, full_booking):
            payout = max(0, booking.online_amount - booking.fee)
            self.assertEqual(payout + booking.at_venue,
                             booking.amount - booking.fee)
