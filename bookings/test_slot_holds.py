"""
The 5-minute slot hold (bookings/holds.py, bookings/hold_views.py).

Venue categories (Celebrate · Gaming · Sports · resort hourly bookings):
  1-3  A holds 3 PM; B is refused with SLOT_HELD and sees it as reserved
  4-5  A pays within 5 minutes on the SAME row; the slot stays taken
  6-8  A abandons; after 5 minutes B gets the slot
  9    two customers racing for one slot (threaded, PostgreSQL only)
  10   refreshing / a second device returns the same hold, same expiry
  11   a failed payment never confirms
  12   a late payment after expiry: confirmed only if still free, else refund
  14   pitches / stations are held independently
Resort packages (13): capacity per date + slot, held for 5 minutes.
Events: unlimited dates take any number of registrations with no lock;
limited dates still never oversell.
Fitness: any number of members can join the same plan.
"""
import datetime
import hashlib
import hmac
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from venues.models import Listing

from . import holds
from .models import Booking
from .slots import today_ist
from .tests import PLAYZONE_RECORD, RECORD

TOMORROW = (today_ist() + datetime.timedelta(days=1)).isoformat()
THREE_PM = '15:00 – 16:00'
KEYS = override_settings(
    RAZORPAY_KEY_ID='rzp_test_key',
    RAZORPAY_KEY_SECRET='test_key_secret',
    RAZORPAY_WEBHOOK_SECRET='test_webhook_secret',
)
# Stored venue categories for Celebrate, Gaming, Sports and Resorts.
CATEGORIES = ('Party hall', 'Playstation', 'Play zone', 'Resort')


def sign(order_id, payment_id):
    return hmac.new(b'test_key_secret', f'{order_id}|{payment_id}'.encode(), hashlib.sha256).hexdigest()


class _HoldBase(APITestCase):
    _seq = 0

    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(phone='9300000001', name='Vendor', role=User.Role.VENDOR)
        self.a = User.objects.create_user(phone='9300000002', name='Asha')
        self.b = User.objects.create_user(phone='9300000003', name='Bala')
        self.venue = self.make_venue('Party hall')

    def tearDown(self):
        cache.clear()

    def make_venue(self, category, record=None):
        listing_id = uuid.uuid4()
        return Listing.objects.create(
            id=listing_id, vendor=self.vendor, slug=f'v-{listing_id.hex[:8]}',
            record={**(record or RECORD), 'id': str(listing_id), 'status': 'live', 'category': category},
            name=f'{category} venue', category=category, status=Listing.Status.LIVE,
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def body(self, venue=None, **over):
        return {
            'venueId': str((venue or self.venue).id), 'date': TOMORROW, 'slots': [THREE_PM],
            'addons': [], 'perSlot': 600, 'amount': 620, **over,
        }

    def hold(self, user, venue=None, **over):
        return self.client_for(user).post('/api/bookings/holds', self.body(venue, **over), format='json')

    def order(self, user, venue=None, **over):
        _HoldBase._seq += 1
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': f'order_HOLD{_HoldBase._seq}'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            return self.client_for(user).post('/api/payments/order', self.body(venue, **over), format='json')

    def verify(self, user, order):
        payment_id = f'pay_{order.data["orderId"]}'
        return self.client_for(user).post('/api/payments/verify', {
            'bookingId': order.data['bookingId'], 'razorpay_order_id': order.data['orderId'],
            'razorpay_payment_id': payment_id, 'razorpay_signature': sign(order.data['orderId'], payment_id),
        }, format='json')

    def expire(self, booking_id):
        Booking.objects.filter(pk=booking_id).update(
            hold_expires_at=timezone.now() - datetime.timedelta(seconds=1),
        )

    def availability(self, user=None, venue=None):
        client = self.client_for(user) if user else APIClient()
        return client.get(f'/api/venues/{(venue or self.venue).id}/availability?date={TOMORROW}').data


@KEYS
class HoldLifecycleTests(_HoldBase):
    def test_a_hold_lasts_five_minutes_by_the_server_clock(self):
        response = self.hold(self.a)
        self.assertEqual(response.status_code, 201, response.data)
        hold = response.data['hold']
        self.assertEqual(hold['state'], 'held')
        self.assertTrue(295 <= hold['secondsLeft'] <= 300)
        row = Booking.objects.get(pk=hold['id'])
        self.assertEqual(row.status, 'payment_pending')
        self.assertEqual(row.razorpay_order_id, '')
        self.assertAlmostEqual(
            (row.hold_expires_at - row.created_at).total_seconds(), 300, delta=2,
        )

    def test_scenarios_1_to_5_across_every_category(self):
        for category in CATEGORIES:
            with self.subTest(category=category):
                venue = self.make_venue(category)
                held = self.hold(self.a, venue)
                self.assertEqual(held.status_code, 201, held.data)
                # B sees it reserved (not booked) and cannot take it.
                seen = self.availability(self.b, venue)
                self.assertEqual(seen['held'], [THREE_PM])
                self.assertIn(THREE_PM, seen['booked'])
                refused = self.hold(self.b, venue)
                self.assertEqual(refused.status_code, 409)
                self.assertEqual(refused.data['code'], 'SLOT_HELD')
                self.assertIn('temporarily reserved', refused.data['message'].lower())
                self.assertEqual(self.order(self.b, venue).status_code, 409)
                # A's own view does not show A's hold as taken.
                self.assertEqual(self.availability(self.a, venue)['booked'], [])
                # A pays within the 5 minutes, on the SAME row.
                order = self.order(self.a, venue, holdId=held.data['hold']['id'])
                self.assertEqual(order.status_code, 201, order.data)
                self.assertEqual(order.data['bookingId'], held.data['hold']['id'])
                self.assertEqual(order.data['hold']['expiresAt'], held.data['hold']['expiresAt'])
                self.assertEqual(self.verify(self.a, order).status_code, 200)
                self.assertEqual(Booking.objects.get(pk=order.data['bookingId']).status, 'confirmed')
                # Confirmed: still unavailable, now as BOOKED.
                again = self.hold(self.b, venue)
                self.assertEqual((again.status_code, again.data['code']), (409, 'SLOT_BOOKED'))
                self.assertEqual(self.availability(self.b, venue)['held'], [])
                self.assertIn(THREE_PM, self.availability(None, venue)['booked'])

    def test_scenarios_6_to_8_an_abandoned_hold_frees_the_slot(self):
        held = self.hold(self.a).data['hold']
        self.assertEqual(self.hold(self.b).status_code, 409)
        self.expire(held['id'])
        # Free at once — no clean-up job needed.
        self.assertEqual(self.availability()['booked'], [])
        taken = self.hold(self.b)
        self.assertEqual(taken.status_code, 201, taken.data)
        # A's expired hold can no longer be paid for.
        late = self.order(self.a, holdId=held['id'])
        self.assertEqual((late.status_code, late.data['code']), (409, 'HOLD_EXPIRED'))
        status = self.client_for(self.a).get(f'/api/bookings/holds/{held["id"]}').data['hold']
        self.assertEqual((status['state'], status['secondsLeft']), ('expired', 0))

    def test_scenario_10_refresh_or_another_device_never_extends_the_hold(self):
        first = self.hold(self.a).data['hold']
        Booking.objects.filter(pk=first['id']).update(
            hold_expires_at=timezone.now() + datetime.timedelta(seconds=40),
        )
        again = self.hold(self.a)                          # refresh / second device
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.data['hold']['id'], first['id'])
        self.assertLessEqual(again.data['hold']['secondsLeft'], 40)
        # Starting the payment does not extend it either…
        order = self.order(self.a, holdId=first['id'])
        self.assertLessEqual(order.data['hold']['secondsLeft'], 40)
        # …nor does pressing Pay a second time (a new order row, same expiry).
        second = self.order(self.a, holdId=first['id'])
        self.assertEqual(second.status_code, 201, second.data)
        self.assertNotEqual(second.data['bookingId'], first['id'])
        self.assertEqual(Booking.objects.get(pk=first['id']).status, 'cancelled')
        self.assertLessEqual(second.data['hold']['secondsLeft'], 40)
        # Still only one live hold on the slot.
        live = Booking.objects.filter(holds.live_hold_q(), listing=self.venue)
        self.assertEqual(live.count(), 1)

    def test_a_new_choice_at_the_same_venue_replaces_the_old_hold(self):
        first = self.hold(self.a).data['hold']
        second = self.hold(self.a, slots=['17:00 – 18:00'])
        self.assertEqual(second.status_code, 201)
        self.assertEqual(Booking.objects.get(pk=first['id']).status, 'cancelled')
        self.assertEqual(self.hold(self.b).status_code, 201)   # 3 PM free again

    def test_hold_must_match_the_slots_being_paid_for(self):
        held = self.hold(self.a).data['hold']
        response = self.order(self.a, holdId=held['id'], slots=['17:00 – 18:00'])
        self.assertEqual((response.status_code, response.data['code']), (409, 'HOLD_MISMATCH'))

    def test_someone_elses_hold_cannot_be_used_or_seen(self):
        held = self.hold(self.a).data['hold']
        self.assertEqual(self.order(self.b, holdId=held['id']).data['code'], 'HOLD_EXPIRED')
        self.assertEqual(self.client_for(self.b).get(f'/api/bookings/holds/{held["id"]}').status_code, 404)
        self.assertEqual(self.client_for(self.b).delete(f'/api/bookings/holds/{held["id"]}').status_code, 404)
        self.assertEqual(APIClient().post('/api/bookings/holds', self.body(), format='json').status_code, 401)

    def test_release_frees_the_slot_at_once(self):
        held = self.hold(self.a).data['hold']
        response = self.client_for(self.a).delete(f'/api/bookings/holds/{held["id"]}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['hold']['state'], 'released')
        self.assertEqual(self.hold(self.b).status_code, 201)

    def test_one_customer_cannot_hoard_slots(self):
        for _ in range(holds.MAX_LIVE_HOLDS_PER_USER):
            self.assertEqual(self.hold(self.a, self.make_venue('Party hall')).status_code, 201)
        self.assertEqual(self.hold(self.a, self.make_venue('Party hall')).status_code, 429)

    def test_scenario_11_a_failed_payment_never_confirms(self):
        held = self.hold(self.a).data['hold']
        order = self.order(self.a, holdId=held['id'])
        bad = self.client_for(self.a).post('/api/payments/verify', {
            'bookingId': order.data['bookingId'], 'razorpay_order_id': order.data['orderId'],
            'razorpay_payment_id': 'pay_x', 'razorpay_signature': 'forged',
        }, format='json')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(Booking.objects.get(pk=held['id']).status, 'payment_pending')
        # The gateway reports the failure: the slot is released.
        import json
        payload = json.dumps({'event': 'payment.failed', 'payload': {'payment': {'entity': {
            'id': 'pay_x', 'order_id': order.data['orderId']}}}})
        signature = hmac.new(b'test_webhook_secret', payload.encode(), hashlib.sha256).hexdigest()
        APIClient().post('/api/payments/webhook', payload, content_type='application/json',
                         HTTP_X_RAZORPAY_SIGNATURE=signature)
        self.assertEqual(Booking.objects.get(pk=held['id']).status, 'failed')
        self.assertEqual(self.hold(self.b).status_code, 201)

    def test_scenario_12_a_late_payment_never_double_books(self):
        held = self.hold(self.a).data['hold']
        order = self.order(self.a, holdId=held['id'])
        self.expire(held['id'])
        taken = self.order(self.b)
        self.assertEqual(taken.status_code, 201)
        self.assertEqual(self.verify(self.b, taken).status_code, 200)
        late = self.verify(self.a, order)                 # A's payment lands late
        self.assertEqual(late.status_code, 409)
        self.assertEqual(Booking.objects.get(pk=held['id']).status, 'refund_pending')
        confirmed = Booking.objects.filter(listing=self.venue, status='confirmed')
        self.assertEqual(confirmed.count(), 1)

    def test_a_late_payment_for_a_slot_still_free_is_confirmed(self):
        held = self.hold(self.a).data['hold']
        order = self.order(self.a, holdId=held['id'])
        self.expire(held['id'])
        self.assertEqual(self.verify(self.a, order).status_code, 200)
        self.assertEqual(Booking.objects.get(pk=held['id']).status, 'confirmed')

    def test_scenario_14_pitches_are_held_independently(self):
        turf = self.make_venue('Play zone', PLAYZONE_RECORD)
        pitch = {'sport': 'Box Cricket', 'perSlot': 599, 'amount': 619}
        one = self.hold(self.a, turf, unit=1, unitLabel='Box Cricket · Pitch 1', **pitch)
        self.assertEqual(one.status_code, 201, one.data)
        two = self.hold(self.b, turf, unit=2, unitLabel='Box Cricket · Pitch 2', **pitch)
        self.assertEqual(two.status_code, 201, two.data)
        same = self.hold(self.b, turf, unit=1, unitLabel='Box Cricket · Pitch 1', **pitch)
        self.assertEqual(same.data['code'], 'SLOT_HELD')
        # A refused attempt never costs B the pitch B already holds.
        self.assertEqual(Booking.objects.get(pk=two.data['hold']['id']).status, 'payment_pending')
        seen = self.availability(None, turf)
        self.assertEqual({u['unit'] for u in seen['heldUnits']}, {1, 2})

    def test_legacy_rows_without_a_stored_expiry_use_created_at(self):
        held = self.hold(self.a).data['hold']
        Booking.objects.filter(pk=held['id']).update(
            hold_expires_at=None, created_at=timezone.now() - datetime.timedelta(minutes=6),
        )
        self.assertEqual(self.hold(self.b).status_code, 201)

    def test_admin_and_record_show_the_hold_state(self):
        held = self.hold(self.a).data['hold']
        row = Booking.objects.get(pk=held['id'])
        self.assertEqual(row.as_record()['holdState'], 'held')
        self.assertIsNotNone(row.as_record()['holdExpiresAt'])
        self.order(self.a, holdId=held['id'])
        row.refresh_from_db()
        self.assertEqual(row.as_record()['holdState'], 'payment_pending')
        self.expire(held['id'])
        row.refresh_from_db()
        self.assertEqual(row.as_record()['holdState'], 'expired')
        self.assertEqual(row.as_record()['bookingState'], 'expired')

    def test_clean_up_command_only_releases_holds_that_never_reached_the_gateway(self):
        bare = self.hold(self.a).data['hold']
        paying = self.order(self.b, slots=['18:00 – 19:00'])
        self.expire(bare['id'])
        self.expire(paying.data['bookingId'])
        call_command('release_expired_holds', verbosity=0)
        self.assertEqual(Booking.objects.get(pk=bare['id']).status, 'cancelled')
        # Kept payable: a late capture must still find its row.
        self.assertEqual(Booking.objects.get(pk=paying.data['bookingId']).status, 'payment_pending')


@KEYS
class ResortPackageHoldTests(APITestCase):
    """Scenario 13 — a resort package's places per date + slot."""

    def setUp(self):
        from offerings.tests import OfferingsApiTests
        cache.clear()
        self.helper = OfferingsApiTests(methodName='run')
        self.helper.client = self.client
        self.helper.setUp()

    def test_a_package_hold_blocks_for_five_minutes_then_frees_its_places(self):
        from offerings.tests import package
        h = self.helper
        _, lid = h.publish('resort', {'packages': [package(capacity='6')]})
        body = {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pkg-family',
                'date': (today_ist() + datetime.timedelta(days=1)).isoformat(),
                'slot': '09:00-13:00', 'adults': 6}
        first = h.order(h.customer, body)
        self.assertEqual(first.status_code, 201, first.data)
        row = Booking.objects.get(pk=first.data['bookingId'])
        self.assertAlmostEqual((row.hold_expires_at - row.created_at).total_seconds(), 300, delta=2)
        self.assertEqual(first.data['hold']['state'], 'payment_pending')
        refused = h.order(h.other, body)                                   # same date + slot, held
        self.assertEqual(refused.status_code, 409)
        self.assertIn('temporarily reserved', refused.data['message'].lower())
        rows = h.client.get(f'/api/venues/{lid}/offerings?date={body["date"]}').data
        self.assertIn('Temporarily reserved', rows['packages'][0]['availability'][0]['reason'])
        Booking.objects.filter(pk=row.pk).update(hold_expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertEqual(h.order(h.other, body).status_code, 201)          # freed after expiry


@KEYS
class EventAndFitnessRegistrationTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_unlimited_event_dates_take_any_number_of_registrations(self):
        from django.urls import reverse

        from events.inventory import reserve_seats, seats_remaining
        from events.tests import auth, make_customer, make_event, make_vendor
        event = make_event(make_vendor(), capacity=None)
        date = event.dates.first()
        self.assertIsNone(seats_remaining(date))
        for index in range(25):
            client = auth(make_customer(f'91000001{index:02d}'))
            with patch('events.payments.create_order', return_value=f'order_U{index}'):
                response = client.post(reverse('event-payment-order'), {
                    'eventId': str(event.id), 'eventDateId': date.id, 'tickets': {'adult': 4},
                }, format='json')
            self.assertEqual(response.status_code, 201, response.data)
        # No lock is taken for an unlimited date.
        with patch('events.inventory.EventDate.objects.select_for_update') as lock:
            reserve_seats(event, date.id, 10)
            lock.assert_not_called()
        public = APIClient().get(f'/api/events/{event.id}').data
        self.assertFalse(public['dates'][0]['soldOut'])

    def test_a_ticket_order_limit_still_applies_to_unlimited_dates(self):
        from django.urls import reverse

        from events.tests import auth, make_customer, make_event, make_vendor
        event = make_event(make_vendor(), capacity=None)
        event.ticket_types.filter(key='adult').update(max_per_order=2)
        with patch('events.payments.create_order', return_value='order_L'):
            response = auth(make_customer('9100000299')).post(reverse('event-payment-order'), {
                'eventId': str(event.id), 'eventDateId': event.dates.first().id, 'tickets': {'adult': 3},
            }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_limited_event_dates_still_never_oversell(self):
        from django.urls import reverse

        from events.tests import auth, make_customer, make_event, make_vendor
        event = make_event(make_vendor(), capacity=5)
        date = event.dates.first()
        codes = []
        for index in range(3):
            with patch('events.payments.create_order', return_value=f'order_C{index}'):
                codes.append(auth(make_customer(f'91000003{index:02d}')).post(reverse('event-payment-order'), {
                    'eventId': str(event.id), 'eventDateId': date.id, 'tickets': {'adult': 2},
                }, format='json').status_code)
        self.assertEqual(codes, [201, 201, 409])

    def test_unlimited_capacity_can_be_saved_and_reverted(self):
        from events.writes import parse_dates
        rows = parse_dates([{'date': TOMORROW, 'unlimited': True}, {'date': (
            today_ist() + datetime.timedelta(days=2)).isoformat(), 'capacity': 50}])
        self.assertEqual([r['capacity'] for r in rows], [None, 50])

    def test_many_members_can_join_the_same_plan(self):
        from offerings.tests import OfferingsApiTests, plan
        helper = OfferingsApiTests(methodName='run')
        helper.client = self.client
        helper.setUp()
        _, lid = helper.publish('fitness', {'plans': [plan()]})
        members = [helper.customer, helper.other] + [
            User.objects.create_user(phone=f'94000001{i:02d}', name=f'M{i}') for i in range(5)
        ]
        body = {'kind': 'fitness', 'venueId': str(lid), 'planId': 'p-month'}
        orders = [helper.order(user, body) for user in members]
        self.assertEqual([o.status_code for o in orders], [201] * len(members))
        self.assertEqual([helper.verify(u, o).status_code for u, o in zip(members, orders)], [200] * len(members))


@KEYS
@unittest.skipUnless(connection.vendor == 'postgresql', 'row locks need PostgreSQL')
class SimultaneousHoldTests(TransactionTestCase):
    """Scenario 9 — two customers press Continue on the same slot at the same
    instant: exactly one gets the hold."""

    def test_exactly_one_of_many_simultaneous_holds_wins(self):
        vendor = User.objects.create_user(phone='9300000101', name='V', role=User.Role.VENDOR)
        listing_id = uuid.uuid4()
        venue = Listing.objects.create(
            id=listing_id, vendor=vendor, slug='race', name='Race hall', category='Party hall',
            record={**RECORD, 'id': str(listing_id), 'status': 'live'}, status=Listing.Status.LIVE,
        )
        users = [User.objects.create_user(phone=f'93000002{i:02d}', name=f'C{i}') for i in range(8)]
        barrier = threading.Barrier(len(users))
        results = []

        def attempt(user):
            client = APIClient()
            client.force_authenticate(user)
            barrier.wait()
            try:
                response = client.post('/api/bookings/holds', {
                    'venueId': str(venue.id), 'date': TOMORROW, 'slots': [THREE_PM],
                    'addons': [], 'perSlot': 600, 'amount': 620,
                }, format='json')
                results.append(response.status_code)
            finally:
                connection.close()

        threads = [threading.Thread(target=attempt, args=(u,)) for u in users]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [201] + [409] * (len(users) - 1))
        self.assertEqual(Booking.objects.filter(holds.live_hold_q(), listing=venue).count(), 1)
