"""
One 6-character booking ID for EVERY booking type.

Venue bookings (every category, online and walk-in) and event ticket orders
are issued the same kind of ID, unique across all of them, and that exact ID
is what the customer sees, the vendor verifies and the super admin looks up.
Older 'bk_…' / 'eo_…' IDs are left exactly as they were.
"""
import datetime
import hashlib
import hmac
import importlib
import re
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.db import IntegrityError
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User
from events.models import Event, EventDate, EventOrder, TicketType

from . import booking_codes, checkin
from .models import Booking, BookingCode
from .slots import today_ist
from .test_checkin import CheckInTestCase, jwt_client, make_booking, make_venue

CODE = re.compile(r'^[A-Z0-9]{6}$')
TODAY = today_ist()
TOMORROW = (TODAY + datetime.timedelta(days=1)).isoformat()
# The venue categories the platform supports (venues/taxonomy.py), plus the
# single-sport names play zones are stored under.
CATEGORIES = ('Party hall', 'Private theatre', 'Play zone', 'Resort', 'Playstation',
              'Box cricket', 'Swimming pool', 'Pickleball')


def make_event_order(event, customer, **extra):
    fields = dict(
        event=event, event_date=event.dates.first(), user=customer, event_name=event.name,
        date=TODAY, customer_name=customer.name, phone=customer.phone, status='confirmed',
        items=[{'key': 'adult', 'label': 'Adult', 'qty': 2, 'price': 300}],
        seats=2, ticket_count=2, subtotal=600, fee=20, amount=620,
    )
    fields.update(extra)
    return EventOrder.objects.create(**fields)


class SharedIdTestCase(CheckInTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            phone='9900000301', name='Admin', email='a@example.com',
            role=User.Role.ADMIN, is_staff=True, password='x',
        )
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)
        self.event = Event.objects.create(
            slug='abc-garba', name='ABC Garba', category='Dandiya', listing=self.abc,
            start_time=datetime.time(18, 0), end_time=datetime.time(22, 0), status=Event.Status.LIVE,
        )
        EventDate.objects.create(event=self.event, date=TODAY, capacity=100)
        TicketType.objects.create(event=self.event, key='adult', label='Adult', price=300)

    def admin_verify(self, booking_id):
        return self.admin_client.post('/api/admin/check-ins/verify', {'bookingId': booking_id}, format='json').data

    def assert_registered(self, booking_id, kind):
        self.assertRegex(booking_id, CODE)
        self.assertEqual(BookingCode.objects.get(pk=booking_id).kind, kind)


class EveryBookingTypeTests(SharedIdTestCase):

    def test_every_venue_category_gets_the_shared_id_and_verifies_by_it(self):
        for category in CATEGORIES:
            venue = make_venue(self.abc_vendor, f'ABC {category}')
            Listing = type(venue)
            Listing.objects.filter(pk=venue.pk).update(category=category)
            venue.refresh_from_db()
            booking = make_booking(venue, self.customer_a)
            with self.subTest(category=category):
                self.assert_registered(booking.id, 'venue')
                self.assertEqual(booking.as_record()['id'], booking.id)   # what the customer sees
                r = self.verify(booking.id, venue)                        # what the vendor types
                self.assertEqual(r.data['result']['code'], checkin.VALID)
                self.assertEqual(r.data['result']['booking']['id'], booking.id)
                admin = self.admin_verify(booking.id)                      # what the admin types
                self.assertEqual((admin['found'], admin['booking']['id']), (True, booking.id))

    def test_event_orders_use_the_same_format_and_registry(self):
        order = make_event_order(self.event, self.customer_a)
        self.assert_registered(order.id, 'event')
        r = self.verify(order.id, eventId=str(self.event.id))
        self.assertEqual(r.data['result']['code'], checkin.VALID)
        self.assertEqual(self.admin_verify(order.id)['booking']['kind'], 'event')

    def test_walk_in_bookings_get_the_shared_id(self):
        r = self.vendor.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.abc.id), 'date': TOMORROW, 'slots': ['19:30 – 21:00'],
            'customer': 'Door customer', 'perSlot': 600, 'amount': 900,
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assert_registered(r.data['booking']['id'], 'venue')

    @override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='secret_k')
    def test_online_booking_keeps_one_id_from_order_to_confirmation(self):
        customer = jwt_client(self.customer_b)
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': 'order_SHARED1'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            r = customer.post('/api/payments/order', {
                'venueId': str(self.abc.id), 'date': TOMORROW, 'slots': ['19:30 – 21:00'],
                'addons': [], 'amount': 920, 'perSlot': 600,
            }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        booking_id = r.data['bookingId']
        self.assert_registered(booking_id, 'venue')
        signature = hmac.new(b'secret_k', b'order_SHARED1|pay_S1', hashlib.sha256).hexdigest()
        payload = {'bookingId': booking_id, 'razorpay_order_id': 'order_SHARED1',
                   'razorpay_payment_id': 'pay_S1', 'razorpay_signature': signature}
        for _ in range(2):   # a repeated confirmation callback
            r = customer.post('/api/payments/verify', payload, format='json')
            self.assertEqual(r.data['booking']['id'], booking_id)         # the success screen's ID
        self.assertEqual(Booking.objects.filter(razorpay_order_id='order_SHARED1').count(), 1)
        listed = jwt_client(self.customer_b).get('/api/users/me/bookings').data['bookings']
        self.assertIn(booking_id, [b['id'] for b in listed])             # My bookings
        self.assertEqual(BookingCode.objects.filter(pk=booking_id).count(), 1)

    def test_a_valid_id_never_marks_a_booking_paid(self):
        unpaid = make_booking(self.abc, self.customer_a, status='payment_pending')
        self.assertEqual(self.check_in(unpaid.id, self.abc).data['result']['code'], checkin.PAYMENT_INCOMPLETE)
        self.assertEqual(self.admin_verify(unpaid.id)['code'], checkin.PAYMENT_INCOMPLETE)
        unpaid.refresh_from_db()
        self.assertEqual((unpaid.status, unpaid.checked_in_at), ('payment_pending', None))

    def test_rules_hold_for_six_character_ids(self):
        for status, code in (('cancelled', checkin.CANCELLED), ('refunded', checkin.REFUNDED),
                             ('failed', checkin.PAYMENT_INCOMPLETE)):
            booking = make_booking(self.abc, self.customer_a, status=status)
            self.assertEqual(self.check_in(booking.id, self.abc).status_code, 409)
            self.assertEqual(self.verify(booking.id, self.abc).data['result']['code'], code)
        self.assertEqual(self.verify(self.booking_b.id, self.abc).data['result']['code'], checkin.WRONG_VENUE)
        self.assertEqual(self.check_in(self.booking_a.id, self.abc).status_code, 200)
        self.assertEqual(self.check_in(self.booking_a.id, self.abc).data['code'], checkin.ALREADY_CHECKED_IN)
        self.assertEqual(self.verify('ZZZZZZ', self.abc).data['result']['code'], checkin.INVALID_ID)


class UniquenessTests(SharedIdTestCase):

    def test_ids_are_unique_across_venue_bookings_and_event_orders(self):
        ids = [make_booking(self.abc, self.customer_a).id for _ in range(80)]
        ids += [make_event_order(self.event, self.customer_a).id for _ in range(80)]
        ids += [self.booking_a.id, self.booking_b.id]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(set(ids) <= set(BookingCode.objects.values_list('code', flat=True)))

    def test_a_code_held_by_an_event_order_is_never_reused_for_a_venue_booking(self):
        order = make_event_order(self.event, self.customer_a)
        with patch('bookings.booking_codes.new_code', side_effect=[order.id, 'K7M2QX']):
            booking = Booking(**self.fields())
            booking.id = order.id            # as if the default drew it
            booking._generated_id = True
            booking.save()
        self.assertEqual(booking.id, 'K7M2QX')

    def test_a_concurrent_claim_is_caught_by_the_database(self):
        """Another booking type claims the code between our check and our
        insert: the BookingCode primary key rejects it and we retry."""
        BookingCode.objects.create(code='K7M2QX', kind='event')   # the other request's claim
        with patch('bookings.booking_codes.code_in_use', side_effect=[False, True, False]), \
             patch('bookings.booking_codes.new_code', return_value='P3RT9W'):
            booking = Booking(**self.fields())
            booking.id = 'K7M2QX'
            booking._generated_id = True
            booking.save()
        self.assertEqual(booking.id, 'P3RT9W')
        self.assertFalse(Booking.objects.filter(pk='K7M2QX').exists())

    def test_gives_up_loudly_rather_than_duplicate(self):
        with patch('bookings.booking_codes.new_code', return_value=self.booking_b.id):
            booking = Booking(**self.fields())
            booking.id = self.booking_b.id
            booking._generated_id = True
            with self.assertRaises(IntegrityError):
                booking.save()

    def test_updates_never_change_an_id(self):
        original = self.booking_a.id
        self.booking_a.status = 'cancelled'
        self.booking_a.save(update_fields=['status'])
        self.booking_a.save()
        self.assertEqual(Booking.objects.get(pk=original).status, 'cancelled')
        self.assertEqual(BookingCode.objects.filter(pk=original).count(), 1)

    def test_older_ids_are_untouched_and_not_registered(self):
        legacy = make_booking(self.abc, self.customer_a, id='bk_0123456789ab')
        self.assertEqual(legacy.id, 'bk_0123456789ab')
        self.assertFalse(BookingCode.objects.filter(pk=legacy.id).exists())
        self.assertEqual(self.verify(legacy.id, self.abc).data['result']['code'], checkin.VALID)

    def fields(self):
        return dict(listing=self.abc, user=self.customer_a, venue_name='ABC Venue', date=TODAY,
                    slots=['10:00 – 11:00'], amount=620, status='confirmed')


class MigrationTests(SharedIdTestCase):
    migration = importlib.import_module('bookings.migrations.0018_booking_codes')

    def test_backfill_registers_existing_six_character_ids(self):
        order = make_event_order(self.event, self.customer_a)
        BookingCode.objects.all().delete()               # the pre-migration state
        self.migration.register_existing_codes(django_apps, None)
        self.assertEqual(BookingCode.objects.get(pk=order.id).kind, 'event')
        self.assertEqual(BookingCode.objects.get(pk=self.booking_a.id).kind, 'venue')

    def test_backfill_refuses_an_existing_cross_type_collision(self):
        BookingCode.objects.all().delete()
        Booking.objects.bulk_create([Booking(id='WXYZ23', listing=self.abc, date=TODAY, amount=1)])
        EventOrder.objects.bulk_create([EventOrder(id='WXYZ23', event=self.event, date=TODAY)])
        with self.assertRaisesRegex(RuntimeError, 'WXYZ23'):
            self.migration.register_existing_codes(django_apps, None)
        self.assertEqual(BookingCode.objects.count(), 0)   # nothing half-written


class GeneratorTests(SharedIdTestCase):
    def test_format(self):
        for _ in range(3000):
            code = booking_codes.new_code()
            self.assertRegex(code, CODE)
            self.assertFalse(set(code) & set('01OIL'), code)              # no look-alikes
            self.assertFalse(re.fullmatch(r'[0-9A-F]{6}', code), code)    # never a legacy invoice ref
