"""
Booking verification and check-in at the gate.

Each class states the business rule it protects. The headline one: a valid
booking ID alone must never open every gate — it opens exactly one venue (or
event), on one day, once.
"""
import datetime
import hashlib
import hmac
import json
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from events.models import Event, EventDate, EventOrder, TicketType
from venues.models import Listing

from . import checkin
from .models import Booking
from .slots import today_ist
from .tests import RECORD

TODAY = today_ist()
VERIFY = '/api/vendors/me/check-ins/verify'
CHECK_IN = '/api/vendors/me/check-ins'
ADMIN_VERIFY = '/api/admin/check-ins/verify'


def jwt_client(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
    return client


def make_vendor(phone, name):
    return User.objects.create_user(phone=phone, name=name, role=User.Role.VENDOR, is_vendor=True)


def make_venue(vendor, name, *, unit_of=None, status=Listing.Status.LIVE):
    detail = {'unitOf': str(unit_of.id)} if unit_of else {}
    return Listing.objects.create(
        id=uuid.uuid4(), vendor=vendor, slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:4]}',
        record={**RECORD, 'name': name, 'detail': {**RECORD['detail'], **detail}},
        name=name, category='Party hall', status=status,
    )


def make_booking(listing, customer, *, status='confirmed', date=TODAY, **extra):
    fields = dict(
        listing=listing, user=customer, venue_name=listing.name, category=listing.category,
        customer_name=customer.name if customer else 'Walk-in',
        phone=customer.phone if customer else '', date=date,
        slots=['19:30 – 21:00'], per_slot=600, amount=920, fee=20,
        method='upi', status=status, razorpay_order_id='order_x', razorpay_payment_id='pay_x',
    )
    fields.update(extra)
    return Booking.objects.create(**fields)


class CheckInTestCase(TestCase):
    """ABC Venue (with a "— Pitch 2" unit) and ABC Lawn belong to vendor
    ABC; XYZ Venue to vendor XYZ. Customer A booked ABC, customer B XYZ."""

    def setUp(self):
        cache.clear()   # throttle counters live in the cache
        self.abc_vendor = make_vendor('9000000201', 'ABC Owner')
        self.xyz_vendor = make_vendor('9000000202', 'XYZ Owner')
        self.customer_a = User.objects.create_user(phone='9100000201', name='Asha Customer')
        self.customer_b = User.objects.create_user(phone='9100000202', name='Bala Customer')
        self.abc = make_venue(self.abc_vendor, 'ABC Venue')
        self.abc_pitch2 = make_venue(self.abc_vendor, 'ABC Venue — Pitch 2', unit_of=self.abc)
        self.abc_lawn = make_venue(self.abc_vendor, 'ABC Lawn')
        self.xyz = make_venue(self.xyz_vendor, 'XYZ Venue')
        self.booking_a = make_booking(self.abc, self.customer_a)
        self.booking_b = make_booking(self.xyz, self.customer_b)
        self.vendor = jwt_client(self.abc_vendor)

    def tearDown(self):
        cache.clear()

    def verify(self, booking_id, venue=None, client=None, **gate):
        body = {'bookingId': booking_id, **gate}
        if venue is not None:
            body['venueId'] = str(venue.id)
        return (client or self.vendor).post(VERIFY, body, format='json')

    def check_in(self, booking_id, venue=None, client=None, **gate):
        body = {'bookingId': booking_id, **gate}
        if venue is not None:
            body['venueId'] = str(venue.id)
        return (client or self.vendor).post(CHECK_IN, body, format='json')


class CorrectVenueTests(CheckInTestCase):
    """Example A — Customer A's confirmed, paid booking at ABC Venue."""

    def test_valid_booking_verifies_then_checks_in_once(self):
        r = self.verify(self.booking_a.id, self.abc)
        self.assertEqual(r.status_code, 200)
        result = r.data['result']
        self.assertEqual(result['code'], checkin.VALID)
        self.assertTrue(result['canCheckIn'])
        self.assertEqual(result['booking']['id'], self.booking_a.id)
        self.assertEqual(result['booking']['customerName'], 'Asha Customer')
        self.assertEqual(result['booking']['phoneLast4'], '••••••0201')   # masked
        self.assertNotIn('phone', result['booking'])
        self.booking_a.refresh_from_db()
        self.assertIsNone(self.booking_a.checked_in_at, 'verify must never write')

        r = self.check_in(self.booking_a.id, self.abc)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['result']['code'], checkin.CHECKED_IN)
        self.booking_a.refresh_from_db()
        self.assertIsNotNone(self.booking_a.checked_in_at)
        self.assertEqual(self.booking_a.checked_in_by, self.abc_vendor)
        self.assertEqual(self.booking_a.as_record()['checkedInAt'],
                         self.booking_a.checked_in_at.isoformat())

    def test_booking_on_a_unit_listing_is_valid_at_its_venue(self):
        pitch_booking = make_booking(self.abc_pitch2, self.customer_a)
        r = self.verify(pitch_booking.id, self.abc)
        self.assertEqual(r.data['result']['code'], checkin.VALID)

    def test_the_six_character_id_can_be_typed_loosely(self):
        code = self.booking_a.id
        self.assertRegex(code, r'^[A-Z0-9]{6}$')
        for typed in (code, code.lower(), f'  {code} ', f'#{code}', f'{code[:3]} {code[3:]}'):
            r = self.verify(typed, self.abc)
            self.assertEqual(r.data['result']['code'], checkin.VALID, typed)

    def test_older_bk_ids_still_verify(self):
        """Bookings made before the shared 6-character format keep their
        original ID — it is never rewritten — and still verify by it."""
        legacy = make_booking(self.abc, self.customer_a, id='bk_3f8a92c1d04e')
        tail = legacy.id[3:]
        for typed in (legacy.id, legacy.id.upper(), f'BK-{tail.upper()}', tail):
            r = self.verify(typed, self.abc)
            self.assertEqual(r.data['result']['code'], checkin.VALID, typed)
            self.assertEqual(r.data['result']['booking']['id'], 'bk_3f8a92c1d04e')

    def test_cash_due_at_the_venue_is_shown_but_entry_allowed(self):
        part = make_booking(self.abc, self.customer_a, pay_now=500, at_venue=420)
        r = self.verify(part.id, self.abc)
        self.assertEqual(r.data['result']['code'], checkin.VALID)
        self.assertEqual(r.data['result']['booking']['dueAtVenue'], 420)
        self.assertIn('₹420', r.data['result']['message'])

    def test_walk_in_recorded_by_the_vendor_is_valid(self):
        walk_in = make_booking(self.abc, None, method='walk-in', walk_in=True, fee=0,
                               razorpay_order_id='', razorpay_payment_id='')
        self.assertEqual(self.verify(walk_in.id, self.abc).data['result']['code'], checkin.VALID)


class WrongVenueTests(CheckInTestCase):
    """Example B — a valid XYZ booking presented at ABC must be refused, and
    must not reveal anything about the XYZ booking."""

    def test_another_vendors_booking_is_invalid_here_and_reveals_nothing(self):
        r = self.verify(self.booking_b.id, self.abc)
        self.assertEqual(r.status_code, 200)
        result = r.data['result']
        self.assertEqual(result['code'], checkin.WRONG_VENUE)
        self.assertEqual(result['title'], 'Invalid for this venue')
        self.assertFalse(result['valid'])
        self.assertIsNone(result['booking'])
        self.assertNotIn('bookedFor', result)
        text = json.dumps(r.data)
        for secret in ('XYZ', 'Bala', '0202', self.booking_b.status, TODAY.isoformat()):
            self.assertNotIn(secret, text, f'leaked {secret!r}')

    def test_another_vendors_booking_cannot_be_checked_in(self):
        r = self.check_in(self.booking_b.id, self.abc)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data['code'], checkin.WRONG_VENUE)
        self.booking_b.refresh_from_db()
        self.assertIsNone(self.booking_b.checked_in_at)

    def test_own_booking_for_another_of_my_venues_names_it(self):
        r = self.verify(self.booking_a.id, self.abc_lawn)
        result = r.data['result']
        self.assertEqual(result['code'], checkin.WRONG_VENUE)
        self.assertEqual(result['bookedFor'], 'ABC Venue')
        self.assertEqual(self.check_in(self.booking_a.id, self.abc_lawn).status_code, 409)

    def test_a_valid_id_never_opens_every_gate(self):
        """The same ID, tried at every gate either vendor has."""
        xyz_client = jwt_client(self.xyz_vendor)
        attempts = [
            (self.vendor, self.abc_lawn), (xyz_client, self.xyz),
        ]
        for client, venue in attempts:
            r = self.check_in(self.booking_a.id, venue, client=client)
            self.assertEqual(r.status_code, 409)
        self.booking_a.refresh_from_db()
        self.assertIsNone(self.booking_a.checked_in_at)


class InvalidIdTests(CheckInTestCase):
    def test_nonexistent_and_malformed_ids(self):
        for typed in ('bk_000000000000', 'bk_zzzz', 'hello world', 'ZZZZZZ', 'x' * 40):
            r = self.verify(typed, self.abc)
            self.assertEqual(r.data['result']['code'], checkin.INVALID_ID, typed)
            self.assertIsNone(r.data['result']['booking'])

    def test_legacy_all_hex_invoice_reference_is_not_matched(self):
        make_booking(self.abc, self.customer_a, id='bk_3f8a92c1d04e')
        for typed in ('BMV-3F8A92', '#3F8A92', '3f8a92'):
            r = self.verify(typed, self.abc)
            self.assertEqual(r.data['result']['code'], checkin.SHORT_REF, typed)

    def test_request_validation(self):
        self.assertEqual(self.vendor.post(VERIFY, {'venueId': str(self.abc.id)}, format='json').status_code, 400)
        self.assertEqual(self.verify('x' * 41, self.abc).status_code, 400)
        self.assertEqual(self.vendor.post(VERIFY, {'bookingId': self.booking_a.id}, format='json').status_code, 400)
        both = {'venueId': str(self.abc.id), 'eventId': str(uuid.uuid4())}
        self.assertEqual(self.verify(self.booking_a.id, **both).status_code, 400)
        self.assertEqual(self.verify(self.booking_a.id, venueId='not-a-uuid').status_code, 404)


class StatusRuleTests(CheckInTestCase):
    def assert_refused(self, booking, code):
        r = self.verify(booking.id, self.abc)
        self.assertEqual(r.data['result']['code'], code, booking.status)
        self.assertFalse(r.data['result']['canCheckIn'])
        r = self.check_in(booking.id, self.abc)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data['result']['code'], code)
        booking.refresh_from_db()
        self.assertIsNone(booking.checked_in_at)

    def test_pending_or_failed_payment(self):
        for status in ('payment_pending', 'failed'):
            self.assert_refused(make_booking(self.abc, self.customer_a, status=status),
                                checkin.PAYMENT_INCOMPLETE)

    def test_cancelled_or_removed(self):
        for status in ('cancelled', 'removed'):
            self.assert_refused(make_booking(self.abc, self.customer_a, status=status), checkin.CANCELLED)

    def test_refunded_or_refund_pending(self):
        for status in ('refunded', 'refund_pending'):
            self.assert_refused(make_booking(self.abc, self.customer_a, status=status), checkin.REFUNDED)

    def test_unknown_status_is_ineligible(self):
        self.assert_refused(make_booking(self.abc, self.customer_a, status='mystery'), checkin.INELIGIBLE)

    def test_wrong_day(self):
        for offset in (1, -1):
            booking = make_booking(self.abc, self.customer_a, date=TODAY + datetime.timedelta(days=offset))
            self.assert_refused(booking, checkin.WRONG_DATE)

    def test_already_checked_in(self):
        self.assertEqual(self.check_in(self.booking_a.id, self.abc).status_code, 200)
        first = Booking.objects.get(pk=self.booking_a.pk).checked_in_at
        r = self.check_in(self.booking_a.id, self.abc)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.data['result']['code'], checkin.ALREADY_CHECKED_IN)
        self.assertEqual(self.verify(self.booking_a.id, self.abc).data['result']['code'],
                         checkin.ALREADY_CHECKED_IN)
        self.assertEqual(Booking.objects.get(pk=self.booking_a.pk).checked_in_at, first)


class AuthorizationTests(CheckInTestCase):
    def test_anonymous_is_401(self):
        r = APIClient().post(VERIFY, {'bookingId': self.booking_a.id, 'venueId': str(self.abc.id)},
                             format='json')
        self.assertEqual(r.status_code, 401)

    def test_customer_account_is_403(self):
        customer = jwt_client(self.customer_a)
        for url in (VERIFY, CHECK_IN):
            r = customer.post(url, {'bookingId': self.booking_a.id, 'venueId': str(self.abc.id)},
                              format='json')
            self.assertEqual(r.status_code, 403, url)
        self.assertEqual(customer.get(CHECK_IN).status_code, 403)

    def test_vendor_cannot_use_another_vendors_venue_as_the_gate(self):
        """Checking 'at XYZ' as the ABC vendor is a 404 — never a verdict."""
        r = self.verify(self.booking_b.id, self.xyz)
        self.assertEqual(r.status_code, 404)
        r = self.check_in(self.booking_b.id, self.xyz)
        self.assertEqual(r.status_code, 404)
        self.booking_b.refresh_from_db()
        self.assertIsNone(self.booking_b.checked_in_at)

    def test_deactivated_vendor_token_is_rejected(self):
        self.abc_vendor.is_active = False
        self.abc_vendor.save(update_fields=['is_active'])
        self.assertEqual(self.verify(self.booking_a.id, self.abc).status_code, 401)

    def test_verification_is_throttled(self):
        from rest_framework.throttling import ScopedRateThrottle
        with patch.object(ScopedRateThrottle, 'THROTTLE_RATES', {'checkin': '3/min'}):
            codes = [self.verify('bk_000000000000', self.abc).status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_audit_log_names_actor_and_booking_but_no_customer_data(self):
        with self.assertLogs('bookings.checkin', level='INFO') as logs:
            self.check_in(self.booking_a.id, self.abc)
        line = logs.output[-1]
        self.assertIn(self.booking_a.id, line)
        self.assertIn(f'user={self.abc_vendor.pk}', line)
        self.assertIn('code=CHECKED_IN', line)
        for pii in ('Asha', self.customer_a.phone):
            self.assertNotIn(pii, line)

    def test_admin_minted_vendor_token_is_attributed(self):
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(self.abc_vendor).access_token
        token['actor'] = 'admin:42'
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        with self.assertLogs('bookings.checkin', level='INFO') as logs:
            self.verify(self.booking_a.id, self.abc, client=client)
        self.assertIn('actor=admin:42', logs.output[-1])


class GateListTests(CheckInTestCase):
    def test_lists_own_venues_without_units_or_deleted(self):
        make_venue(self.abc_vendor, 'Gone Venue', status=Listing.Status.DELETED)
        self.check_in(self.booking_a.id, self.abc)
        r = self.vendor.get(CHECK_IN)
        self.assertEqual(r.status_code, 200)
        names = [g['name'] for g in r.data['gates']]
        self.assertEqual(names, ['ABC Lawn', 'ABC Venue'])
        self.assertEqual(r.data['checkedInToday'], 1)


class SuperAdminVerifyTests(CheckInTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            phone='9900000201', name='Admin', email='admin@example.com',
            role=User.Role.ADMIN, is_staff=True, password='x',
        )
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)   # cookie session, like the panel

    def test_admin_sees_any_booking_across_venues_without_checking_in(self):
        for booking, venue in ((self.booking_a, 'ABC Venue'), (self.booking_b, 'XYZ Venue')):
            r = self.admin_client.post(ADMIN_VERIFY, {'bookingId': booking.id}, format='json')
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data['found'])
            self.assertEqual(r.data['code'], checkin.VALID)
            self.assertTrue(r.data['eligibleToday'])
            self.assertEqual(r.data['booking']['venueName'], venue)
            self.assertEqual(r.data['booking']['status'], 'confirmed')
            booking.refresh_from_db()
            self.assertIsNone(booking.checked_in_at, 'admin verify must never check in')

    def test_admin_sees_status_and_check_in_audit(self):
        self.check_in(self.booking_a.id, self.abc)
        r = self.admin_client.post(ADMIN_VERIFY, {'bookingId': self.booking_a.id}, format='json')
        self.assertEqual(r.data['code'], checkin.ALREADY_CHECKED_IN)
        self.assertEqual(r.data['booking']['checkedInBy']['name'], 'ABC Owner')
        self.assertIsNotNone(r.data['booking']['checkedInAt'])
        refunded = make_booking(self.xyz, self.customer_b, status='refunded', refund_amount=900,
                                refund_reason='Customer request')
        r = self.admin_client.post(ADMIN_VERIFY, {'bookingId': refunded.id}, format='json')
        self.assertEqual(r.data['code'], checkin.REFUNDED)
        self.assertEqual(r.data['booking']['refundAmount'], 900)

    def test_admin_unknown_and_short_ids(self):
        r = self.admin_client.post(ADMIN_VERIFY, {'bookingId': 'bk_000000000000'}, format='json')
        self.assertEqual((r.data['found'], r.data['code']), (False, checkin.INVALID_ID))
        r = self.admin_client.post(ADMIN_VERIFY, {'bookingId': 'BMV-ABC123'}, format='json')
        self.assertEqual(r.data['code'], checkin.SHORT_REF)
        self.assertEqual(self.admin_client.post(ADMIN_VERIFY, {}, format='json').status_code, 400)

    def test_vendor_cannot_reach_admin_verification(self):
        # A vendor's Bearer token is not an admin session…
        r = self.vendor.post(ADMIN_VERIFY, {'bookingId': self.booking_b.id}, format='json')
        self.assertEqual(r.status_code, 403)
        # …and even a logged-in vendor session fails the role check.
        session = APIClient()
        session.force_login(self.abc_vendor)
        r = session.post(ADMIN_VERIFY, {'bookingId': self.booking_b.id}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertEqual(APIClient().post(ADMIN_VERIFY, {'bookingId': 'x'}, format='json').status_code, 403)

    def test_admin_session_cannot_use_vendor_check_in(self):
        r = self.admin_client.post(CHECK_IN, {'bookingId': self.booking_a.id,
                                              'venueId': str(self.abc.id)}, format='json')
        self.assertIn(r.status_code, (401, 403))
        self.booking_a.refresh_from_db()
        self.assertIsNone(self.booking_a.checked_in_at)


class EventTicketTests(CheckInTestCase):
    def setUp(self):
        super().setUp()
        self.event = Event.objects.create(
            slug='abc-dandiya', name='ABC Dandiya', category='Dandiya', venue_name='ABC Venue',
            listing=self.abc, start_time=datetime.time(18, 0), end_time=datetime.time(22, 0),
            status=Event.Status.LIVE,
        )
        date = EventDate.objects.create(event=self.event, date=TODAY, capacity=100)
        TicketType.objects.create(event=self.event, key='adult', label='Adult', price=300)
        self.order = EventOrder.objects.create(
            event=self.event, event_date=date, user=self.customer_a, event_name='ABC Dandiya',
            date=TODAY, customer_name='Asha Customer', phone='9100000201', status='confirmed',
            items=[{'key': 'adult', 'label': 'Adult', 'qty': 3, 'price': 300}],
            seats=3, ticket_count=3, subtotal=900, fee=20, amount=920,
        )
        self.other_event = Event.objects.create(
            slug='xyz-garba', name='XYZ Garba', category='Dandiya', listing=self.xyz,
            start_time=datetime.time(18, 0), end_time=datetime.time(22, 0), status=Event.Status.LIVE,
        )
        self.other_order = EventOrder.objects.create(
            event=self.other_event, user=self.customer_b, event_name='XYZ Garba', date=TODAY,
            status='confirmed', seats=1, ticket_count=1, amount=320,
        )

    def test_ticket_order_checks_in_as_one_party(self):
        r = self.verify(self.order.id.lower(), eventId=str(self.event.id))
        result = r.data['result']
        self.assertEqual(result['code'], checkin.VALID)
        self.assertEqual((result['booking']['tickets'], result['booking']['admits']), (3, 3))
        r = self.check_in(self.order.id, eventId=str(self.event.id))
        self.assertEqual(r.data['result']['code'], checkin.CHECKED_IN)
        self.order.refresh_from_db()
        self.assertEqual(self.order.checked_in_by, self.abc_vendor)
        r = self.check_in(self.order.id, eventId=str(self.event.id))
        self.assertEqual(r.data['result']['code'], checkin.ALREADY_CHECKED_IN)

    def test_ticket_is_valid_at_the_venue_hosting_the_event(self):
        self.assertEqual(self.verify(self.order.id, self.abc).data['result']['code'], checkin.VALID)
        self.assertEqual(self.verify(self.order.id, self.abc_lawn).data['result']['code'],
                         checkin.WRONG_VENUE)

    def test_venue_booking_is_not_an_event_ticket(self):
        r = self.verify(self.booking_a.id, eventId=str(self.event.id))
        self.assertEqual(r.data['result']['code'], checkin.WRONG_VENUE)

    def test_other_vendors_event_ticket_reveals_nothing(self):
        r = self.verify(self.other_order.id, eventId=str(self.event.id))
        self.assertEqual(r.data['result']['code'], checkin.WRONG_VENUE)
        self.assertNotIn('XYZ', json.dumps(r.data))
        # …and their event is not a gate this vendor can pick.
        self.assertEqual(self.verify(self.other_order.id, eventId=str(self.other_event.id)).status_code, 404)

    def test_event_gate_listed(self):
        names = [g['name'] for g in self.vendor.get(CHECK_IN).data['gates']]
        self.assertIn('ABC Dandiya', names)
        self.assertNotIn('XYZ Garba', names)


class RaceTests(CheckInTestCase):
    def test_a_check_in_that_lost_the_race_reports_already_checked_in(self):
        """Simulates the losing request on a database where the row lock did
        not serialise the two (SQLite): it evaluated a stale row as VALID,
        but the conditional UPDATE matches nothing, so it must NOT admit."""
        stale = Booking.objects.get(pk=self.booking_a.pk)                # checked_in_at None
        Booking.objects.filter(pk=self.booking_a.pk).update(            # the winner commits
            checked_in_at=datetime.datetime.now(datetime.timezone.utc), checked_in_by=self.xyz_vendor,
        )
        manager = MagicMock()
        manager.get.return_value = stale
        gate = ('venue', self.abc)
        with patch.object(Booking.objects, 'select_for_update', return_value=manager):
            verdict = checkin.vendor_check_in(self.abc_vendor, self.booking_a.id, gate)
        self.assertEqual(verdict['code'], checkin.ALREADY_CHECKED_IN)
        self.booking_a.refresh_from_db()
        self.assertEqual(self.booking_a.checked_in_by, self.xyz_vendor)   # the winner's record stands

    def test_refund_landing_mid_check_in_blocks_entry(self):
        stale = Booking.objects.get(pk=self.booking_a.pk)
        Booking.objects.filter(pk=self.booking_a.pk).update(status='refunded')
        manager = MagicMock()
        manager.get.return_value = stale
        with patch.object(Booking.objects, 'select_for_update', return_value=manager):
            verdict = checkin.vendor_check_in(self.abc_vendor, self.booking_a.id, ('venue', self.abc))
        self.assertEqual(verdict['code'], checkin.REFUNDED)
        self.booking_a.refresh_from_db()
        self.assertIsNone(self.booking_a.checked_in_at)


@unittest.skipUnless(connection.vendor == 'postgresql', 'needs real row locks (PostgreSQL)')
class ConcurrentCheckInTests(TransactionTestCase):
    """Two doors, one booking, the same instant: exactly one admits."""

    def test_simultaneous_check_ins_admit_once(self):
        vendor = make_vendor('9000000299', 'Door Vendor')
        customer = User.objects.create_user(phone='9100000299', name='C')
        venue = make_venue(vendor, 'Door Venue')
        booking = make_booking(venue, customer)
        barrier = threading.Barrier(2)
        results = []

        def attempt():
            from django.db import connection as thread_connection
            try:
                barrier.wait()
                results.append(checkin.vendor_check_in(vendor, booking.id, ('venue', venue))['code'])
            finally:
                thread_connection.close()

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [checkin.ALREADY_CHECKED_IN, checkin.CHECKED_IN])


RZP_SECRET = 'test_key_secret'
WEBHOOK_SECRET = 'test_webhook_secret'


@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET=RZP_SECRET,
                   RAZORPAY_WEBHOOK_SECRET=WEBHOOK_SECRET)
class PaymentFlowThenCheckInTests(CheckInTestCase):
    """The existing gateway flow is the ONLY source of 'confirmed'. Replayed
    callbacks must not duplicate the booking, and must not disturb a
    check-in that already happened."""

    def test_replayed_verify_and_webhook_then_single_check_in(self):
        customer = jwt_client(self.customer_a)
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': 'order_CHK1'}
        tomorrow = (TODAY + datetime.timedelta(days=1)).isoformat()
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            r = customer.post('/api/payments/order', {
                'venueId': str(self.abc.id), 'date': tomorrow, 'slots': ['19:30 – 21:00'],
                'addons': [], 'amount': 920, 'perSlot': 600,
            }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        booking_id = r.data['bookingId']

        # Not paid yet: the gate refuses it whatever the date.
        Booking.objects.filter(pk=booking_id).update(date=TODAY)
        self.assertEqual(self.verify(booking_id, self.abc).data['result']['code'],
                         checkin.PAYMENT_INCOMPLETE)
        Booking.objects.filter(pk=booking_id).update(date=tomorrow)

        payload = {'bookingId': booking_id, 'razorpay_order_id': 'order_CHK1',
                   'razorpay_payment_id': 'pay_CHK1',
                   'razorpay_signature': hmac.new(RZP_SECRET.encode(), b'order_CHK1|pay_CHK1',
                                                  hashlib.sha256).hexdigest()}
        for _ in range(3):   # the success callback fires more than once
            r = customer.post('/api/payments/verify', payload, format='json')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data['booking']['id'], booking_id)
        self.assertEqual(Booking.objects.filter(razorpay_order_id='order_CHK1').count(), 1)

        # The booking day arrives; the customer is admitted once.
        Booking.objects.filter(pk=booking_id).update(date=TODAY)
        self.assertEqual(self.check_in(booking_id, self.abc).data['result']['code'], checkin.CHECKED_IN)
        checked_at = Booking.objects.get(pk=booking_id).checked_in_at

        # Late duplicate callbacks — verify and the signed webhook.
        self.assertEqual(customer.post('/api/payments/verify', payload, format='json').status_code, 200)
        body = json.dumps({'event': 'payment.captured', 'payload': {'payment': {'entity': {
            'id': 'pay_CHK1', 'order_id': 'order_CHK1', 'amount': 92000}}}}).encode()
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        r = APIClient().post('/api/payments/webhook', body, content_type='application/json',
                             HTTP_X_RAZORPAY_SIGNATURE=signature)
        self.assertEqual(r.status_code, 200)
        booking = Booking.objects.get(pk=booking_id)
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.checked_in_at, checked_at)
        self.assertEqual(Booking.objects.filter(razorpay_order_id='order_CHK1').count(), 1)
        self.assertEqual(self.check_in(booking_id, self.abc).data['result']['code'],
                         checkin.ALREADY_CHECKED_IN)

    def test_customer_sees_check_in_on_their_booking(self):
        self.check_in(self.booking_a.id, self.abc)
        r = jwt_client(self.customer_a).get('/api/users/me/bookings')
        row = next(b for b in r.data['bookings'] if b['id'] == self.booking_a.id)
        self.assertIsNotNone(row['checkedInAt'])
