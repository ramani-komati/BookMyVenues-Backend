"""
Security regression tests — every test here corresponds to a real exploit
found in the full security review, reproduced BEFORE it was fixed.

These are the ones that cost money if they ever come back, so each test
states the attack in plain terms rather than just asserting a status code.
"""
import datetime
import uuid

from django.test import override_settings
from django.utils import timezone
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


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class OfferPerUserLimitTests(GatewayBookingMixin, APITestCase):
    """
    `perUserLimit` on a venue offer. A redemption STANDS while the money is
    kept or in flight; cancelling or refunding gives the allowance back.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9200000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9200000002', name='Asha')
        self.other = User.objects.create_user(phone='9200000003', name='Ravi')
        record = {
            **RECORD, 'id': 'lim', 'status': 'live',
            'detail': {
                **RECORD['detail'],
                'offers': [
                    {'title': 'Once only', 'code': 'ONCE', 'type': 'flat',
                     'value': '100', 'minAmount': '', 'maxDiscount': '',
                     'expiry': '', 'perUserLimit': '1'},
                    {'title': 'Twice', 'code': 'TWICE', 'type': 'flat',
                     'value': '50', 'minAmount': '', 'maxDiscount': '',
                     'expiry': '', 'perUserLimit': '2'},
                    {'title': 'Unlimited', 'code': 'FREEFORALL', 'type': 'flat',
                     'value': '10', 'minAmount': '', 'maxDiscount': '',
                     'expiry': ''},                       # no perUserLimit
                    {'title': 'No code offer', 'code': '', 'type': 'flat',
                     'value': '25', 'minAmount': '', 'maxDiscount': '',
                     'expiry': '', 'perUserLimit': '1'},
                ],
            },
        }
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='lim-hall',
            record=record, name='Limit Hall', category='hall',
            locality='X', pincode='560001',
        )
        self.client.force_authenticate(user=self.customer)

    SLOTS = ['09:00 – 10:00', '11:00 – 12:00', '13:00 – 14:00', '15:00 – 16:00']

    def book(self, code='ONCE', discount=100, slot=0, **overrides):
        body = {
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': [self.SLOTS[slot]], 'addons': [], 'perSlot': 600,
            'amount': 600 - discount + 20,
            'offer': {'code': code},
            **overrides,
        }
        return self.gateway_book(body)

    def _confirm_all(self):
        Booking.objects.filter(status='payment_pending').update(status='confirmed')

    def test_second_use_is_refused_with_the_agreed_code(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        second = self.book(slot=1)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data['code'], 'OFFER_LIMIT_REACHED')
        self.assertIn('maximum number of times', second.data['message'])

    def test_limit_of_two_allows_exactly_two(self):
        self.assertEqual(self.book(code='TWICE', discount=50, slot=0).status_code, 201)
        self._confirm_all()
        self.assertEqual(self.book(code='TWICE', discount=50, slot=1).status_code, 201)
        self._confirm_all()
        third = self.book(code='TWICE', discount=50, slot=2)
        self.assertEqual(third.status_code, 400)
        self.assertEqual(third.data['code'], 'OFFER_LIMIT_REACHED')

    def test_no_limit_set_means_unlimited(self):
        for slot in range(3):
            r = self.book(code='FREEFORALL', discount=10, slot=slot)
            self.assertEqual(r.status_code, 201)
            self._confirm_all()

    def test_the_cap_is_per_user_not_global(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_cancelling_gives_the_allowance_back(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        Booking.objects.update(status='cancelled')
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_a_refund_gives_the_allowance_back(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        Booking.objects.update(status='refunded')
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_a_live_unpaid_hold_still_counts(self):
        """Otherwise several checkout tabs would each see a count of zero and
        every one of them would slip past a limit of 1."""
        self.assertEqual(self.book(slot=0).status_code, 201)   # payment_pending
        second = self.book(slot=1)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data['code'], 'OFFER_LIMIT_REACHED')

    def test_an_expired_hold_stops_counting(self):
        """A failed payment must not burn the allowance forever."""
        self.assertEqual(self.book(slot=0).status_code, 201)
        stale = timezone.now() - datetime.timedelta(minutes=90)
        Booking.objects.filter(status='payment_pending').update(created_at=stale)
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_code_less_offers_are_limited_by_title(self):
        self.assertEqual(self.book(code='', discount=25, slot=0).status_code, 201)
        self._confirm_all()
        second = self.book(code='', discount=25, slot=1)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data['code'], 'OFFER_LIMIT_REACHED')

    def test_walk_ins_are_ignored_by_the_cap(self):
        """Vendor-entered walk-ins have no customer account to attribute."""
        Booking.objects.create(
            listing=self.listing, user=None, venue_name='Limit Hall',
            category='hall', location='x', image='', customer_name='Offline',
            phone='', date=datetime.date.fromisoformat(TOMORROW),   # must match the request body
            slots=['08:00 – 09:00'], per_slot=600, addons=[], fee=0,
            amount=500, method='walk-in', walk_in=True, status='confirmed',
            offer={'code': 'ONCE', 'title': 'Once only', 'source': 'venue'},
        )
        self.assertEqual(self.book(slot=0).status_code, 201)

    def test_a_different_offer_has_its_own_allowance(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        self.assertEqual(self.book(code='TWICE', discount=50, slot=1).status_code, 201)


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class PackagePricingTests(GatewayBookingMixin, APITestCase):
    """
    A package buys a fixed block of time, so its price REPLACES the hourly
    slot charge. Charging both bills the customer twice for one booking.
    """

    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9300000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9300000002', name='Asha')
        record = {
            **RECORD, 'id': 'pkg', 'status': 'live',
            'price': 600,                      # ₹600/hour if booked hourly
            'detail': {
                'addons': [{'name': 'Cake', 'price': 500}],
                'extraPersonPrice': '200', 'maxExtraPersons': '4',
                'packages': [
                    # 3 hours for ₹5000 — hourly would be ₹1800.
                    {'label': 'Birthday Deluxe', 'price': 5000, 'duration': '3'},
                    {'label': 'Quick Hour', 'price': 900, 'duration': '1'},
                    {'label': 'No Duration', 'price': 1500},   # legacy shape
                ],
            },
        }
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='pkg-hall',
            record=record, name='Package Hall', category='hall',
            locality='X', pincode='560001',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, slots, addons, amount):
        return self.gateway_book({
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': slots, 'addons': addons, 'perSlot': 600, 'amount': amount,
        })

    def test_package_price_replaces_the_hourly_charge(self):
        """REGRESSION: this used to charge ₹5000 + ₹1800 for the same hours."""
        r = self.book(
            ['18:00 – 21:00'],                       # exactly 3 hours
            [{'name': 'Package — Birthday Deluxe', 'qty': 1}],
            5020,                                    # 5000 + ₹20 fee, NO 1800
        )
        self.assertEqual(r.status_code, 201, r.data)
        booking = Booking.objects.get(pk=r.data['bookingId'])
        self.assertEqual(booking.amount, 5020)

    def test_the_old_double_charged_total_is_now_refused(self):
        r = self.book(
            ['18:00 – 21:00'],
            [{'name': 'Package — Birthday Deluxe', 'qty': 1}],
            6820,                                    # 5000 + 1800 + 20
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(r.data['expectedAmount'], 5020)

    def test_addons_and_extra_persons_stack_on_the_package(self):
        # 5000 package + 500 cake + (2 x 200) extra persons + 20 fee
        r = self.book(
            ['18:00 – 21:00'],
            [{'name': 'Package — Birthday Deluxe', 'qty': 1},
             {'name': 'Cake', 'qty': 1},
             {'name': 'Extra persons', 'qty': 2}],
            5920,
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_booking_longer_than_the_package_is_refused(self):
        """No extra hours beyond the package duration."""
        r = self.book(
            ['18:00 – 22:00'],                       # 4h against a 3h package
            [{'name': 'Package — Birthday Deluxe', 'qty': 1}],
            5020,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('3 hours', r.data['message'])

    def test_booking_shorter_than_the_package_is_refused(self):
        r = self.book(
            ['18:00 – 20:00'],                       # 2h against a 3h package
            [{'name': 'Package — Birthday Deluxe', 'qty': 1}],
            5020,
        )
        self.assertEqual(r.status_code, 400)

    def test_only_one_package_per_booking(self):
        r = self.book(
            ['18:00 – 21:00'],
            [{'name': 'Package — Birthday Deluxe', 'qty': 2}],
            10020,
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('one package', r.data['message'])

    def test_a_package_without_a_duration_still_drops_the_hourly_charge(self):
        """Legacy packages carry no duration — price still replaces the rate,
        we just cannot check the length."""
        r = self.book(
            ['18:00 – 20:00'],
            [{'name': 'Package — No Duration', 'qty': 1}],
            1520,                                    # 1500 + 20, no hourly
        )
        self.assertEqual(r.status_code, 201, r.data)

    def test_bookings_without_a_package_are_unchanged(self):
        # 2h x 600 + 500 cake + 20 fee
        r = self.book(['18:00 – 20:00'], [{'name': 'Cake', 'qty': 1}], 1720)
        self.assertEqual(r.status_code, 201, r.data)

    def test_the_package_still_locks_the_slot(self):
        first = self.book(
            ['18:00 – 21:00'],
            [{'name': 'Package — Birthday Deluxe', 'qty': 1}], 5020,
        )
        self.assertEqual(first.status_code, 201)
        clash = self.book(
            ['19:00 – 20:00'], [], 620,              # overlaps the package
        )
        self.assertEqual(clash.status_code, 409)


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
)
class PlatformPromoPerUserLimitTests(GatewayBookingMixin, APITestCase):
    """
    `perUserLimit` on an admin banner promo. Unlike a venue offer this is
    PLATFORM-WIDE: one campaign, so a limit of 1 means once per person
    overall — not once per venue, which could be farmed across listings.
    """

    def setUp(self):
        from adminpanel.models import Settings

        self.vendor = User.objects.create_user(
            phone='9400000001', name='Vendor', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9400000002', name='Asha')
        self.other = User.objects.create_user(phone='9400000003', name='Ravi')
        record = {**RECORD, 'id': 'pp', 'status': 'live', 'price': 600,
                  'detail': {'addons': []}}
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='pp-hall',
            record=record, name='Promo Hall', category='hall',
            locality='X', pincode='560001',
        )
        # A SECOND venue — a platform cap must hold across venues.
        self.other_listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='pp-hall-2',
            record={**record, 'id': 'pp2'}, name='Promo Hall Two',
            category='hall', locality='X', pincode='560002',
        )
        today = today_ist()
        s = Settings.load()
        s.banners = [
            {'id': 1, 'title': 'One Shot', 'type': 'flat', 'value': 100,
             'code': 'ONESHOT', 'minAmount': '', 'maxDiscount': '',
             'from': today.isoformat(), 'to': today.isoformat(),
             'perUserLimit': '1'},
            {'id': 2, 'title': 'Open Promo', 'type': 'flat', 'value': 50,
             'code': 'OPEN', 'minAmount': '', 'maxDiscount': '',
             'from': today.isoformat(), 'to': today.isoformat()},   # no limit
        ]
        s.save()
        self.client.force_authenticate(user=self.customer)

    SLOTS = ['09:00 – 10:00', '11:00 – 12:00', '13:00 – 14:00']

    def book(self, code='ONESHOT', discount=100, slot=0, listing=None):
        return self.gateway_book({
            'venueId': str((listing or self.listing).id), 'date': TOMORROW,
            'slots': [self.SLOTS[slot]], 'addons': [], 'perSlot': 600,
            'amount': 600 - discount + 20,
            'offer': {'code': code, 'source': 'platform'},
        })

    def _confirm_all(self):
        Booking.objects.filter(status='payment_pending').update(status='confirmed')

    def test_second_use_is_refused(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        second = self.book(slot=1)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data['code'], 'OFFER_LIMIT_REACHED')
        self.assertIn('maximum number of times', second.data['message'])

    def test_the_cap_holds_across_DIFFERENT_venues(self):
        """The whole point of a platform-wide cap."""
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        elsewhere = self.book(slot=1, listing=self.other_listing)
        self.assertEqual(elsewhere.status_code, 400)
        self.assertEqual(elsewhere.data['code'], 'OFFER_LIMIT_REACHED')

    def test_banner_without_a_limit_is_unlimited(self):
        for slot in range(3):
            r = self.book(code='OPEN', discount=50, slot=slot)
            self.assertEqual(r.status_code, 201, r.data)
            self._confirm_all()

    def test_the_cap_is_per_user(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        self._confirm_all()
        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_cancelling_gives_the_allowance_back(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        Booking.objects.update(status='cancelled')
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_a_live_hold_still_counts(self):
        self.assertEqual(self.book(slot=0).status_code, 201)   # payment_pending
        second = self.book(slot=1)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data['code'], 'OFFER_LIMIT_REACHED')

    def test_an_expired_hold_stops_counting(self):
        self.assertEqual(self.book(slot=0).status_code, 201)
        stale = timezone.now() - datetime.timedelta(minutes=90)
        Booking.objects.filter(status='payment_pending').update(created_at=stale)
        self.assertEqual(self.book(slot=1).status_code, 201)

    def test_platform_and_venue_allowances_stay_separate(self):
        """A banner promo must not eat a venue offer's allowance."""
        self.listing.record = {
            **self.listing.record,
            'detail': {'addons': [], 'offers': [
                {'title': 'Venue one', 'code': 'ONESHOT', 'type': 'flat',
                 'value': '100', 'minAmount': '', 'maxDiscount': '',
                 'expiry': '', 'perUserLimit': '1'},
            ]},
        }
        self.listing.save(update_fields=['record'])

        self.assertEqual(self.book(slot=0).status_code, 201)   # platform ONESHOT
        self._confirm_all()
        # Same code, but the VENUE's own offer — a separate pot.
        venue = self.gateway_book({
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': [self.SLOTS[1]], 'addons': [], 'perSlot': 600,
            'amount': 520, 'offer': {'code': 'ONESHOT'},
        })
        self.assertEqual(venue.status_code, 201, venue.data)
