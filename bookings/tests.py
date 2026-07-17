"""
Tests for bookings + availability (contract 1.3, 2.3, 2.4, 2.5).
"""
import datetime
import uuid

from django.test import TestCase
from rest_framework.test import APITestCase

from accounts.models import User
from venues.models import Listing

from .models import Booking
from .slots import SlotError, overlaps, parse_slot, parse_slots, today_ist

TOMORROW = (today_ist() + datetime.timedelta(days=1)).isoformat()

RECORD = {
    'name': 'Grand Palace Hall',
    'category': 'hall',
    'locality': 'Indiranagar',
    'location': 'Indiranagar, Bengaluru',
    'price': 600,  # hourly rate
    'unit': 'hour',
    'image': 'https://cdn.example/cover.jpg',
    'gallery': ['https://cdn.example/1.jpg'],
    'detail': {
        'addons': [
            {'name': 'Photographer', 'price': 2000},
            {'name': 'Cake', 'price': 500},
        ],
    },
}


class SlotParsingTests(TestCase):
    def test_valid_slot_en_dash(self):
        self.assertEqual(parse_slot('19:30 – 21:00'), (1170, 1260))

    def test_valid_slot_plain_hyphen(self):
        self.assertEqual(parse_slot('06:00 - 07:30'), (360, 450))

    def test_midnight_end(self):
        self.assertEqual(parse_slot('23:30 – 00:00'), (1410, 1440))

    def test_before_opening_rejected(self):
        with self.assertRaises(SlotError):
            parse_slot('05:00 – 06:00')

    def test_non_half_hour_rejected(self):
        with self.assertRaises(SlotError):
            parse_slot('19:15 – 20:00')

    def test_under_30_minutes_rejected(self):
        with self.assertRaises(SlotError):
            parse_slot('19:30 – 19:30')

    def test_garbage_rejected(self):
        with self.assertRaises(SlotError):
            parse_slot('7pm to 9pm')

    def test_slots_overlapping_each_other_rejected(self):
        with self.assertRaises(SlotError):
            parse_slots(['19:00 – 21:00', '20:30 – 22:00'])

    def test_overlap_detection(self):
        booked = [parse_slot('19:30 – 21:00')]
        self.assertTrue(overlaps([parse_slot('20:00 – 22:00')], booked))
        self.assertFalse(overlaps([parse_slot('21:00 – 22:00')], booked))  # touching is fine


class BookingTestBase(APITestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000001', name='Vendor', email='v@example.com',
            role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9000000002', name='Asha', email='a@example.com',
        )
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-palace-hall',
            record={**RECORD, 'id': 'x', 'status': 'live'},
            name='Grand Palace Hall', category='hall',
            locality='Indiranagar', pincode='560038',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, **overrides):
        body = {
            'venueId': str(self.listing.id),
            'date': TOMORROW,
            'slots': ['19:30 – 21:00'],   # 1.5h x 600 = 900
            'addons': [],
            'amount': 920,                # 900 + ₹20 fee
            'perSlot': 600,
            **overrides,
        }
        return self.client.post('/api/users/me/bookings', body, format='json')


class CreateBookingTests(BookingTestBase):
    def test_happy_path(self):
        response = self.book()
        self.assertEqual(response.status_code, 201)
        booking = response.data['booking']
        self.assertEqual(booking['amount'], 920)
        self.assertEqual(booking['venueName'], 'Grand Palace Hall')
        self.assertEqual(booking['customer'], 'Asha')
        self.assertEqual(booking['phone'], '9000000002')
        self.assertTrue(booking['id'].startswith('bk_'))

    def test_amount_with_addons(self):
        response = self.book(
            addons=[{'name': 'Photographer', 'qty': 1}, {'name': 'Cake', 'qty': 2}],
            amount=920 + 2000 + 1000,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['amount'], 3920)
        # Server pinned the real prices from the listing:
        self.assertEqual(response.data['booking']['addons'][0]['price'], 2000)

    def test_wrong_amount_rejected(self):
        response = self.book(amount=100)  # trying to underpay
        self.assertEqual(response.status_code, 400)
        self.assertIn('Amount mismatch', response.data['message'])

    def test_client_addon_price_ignored(self):
        # Client claims the photographer costs ₹1 — server must refuse.
        response = self.book(
            addons=[{'name': 'Photographer', 'qty': 1, 'price': 1}],
            amount=920 + 1,
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_addon_rejected(self):
        response = self.book(addons=[{'name': 'Helicopter', 'qty': 1}], amount=999)
        self.assertEqual(response.status_code, 400)

    def test_overlap_conflict_409(self):
        self.book()
        response = self.book(slots=['20:00 – 22:00'], amount=1220)
        self.assertEqual(response.status_code, 409)

    def test_adjacent_slot_allowed(self):
        self.book()
        response = self.book(slots=['21:00 – 22:00'], amount=620)
        self.assertEqual(response.status_code, 201)

    def test_past_date_rejected(self):
        yesterday = (today_ist() - datetime.timedelta(days=1)).isoformat()
        response = self.book(date=yesterday)
        self.assertEqual(response.status_code, 400)

    def test_bad_slot_rejected(self):
        response = self.book(slots=['25:00 – 26:00'])
        self.assertEqual(response.status_code, 400)

    def test_unknown_venue_404(self):
        response = self.book(venueId=str(uuid.uuid4()))
        self.assertEqual(response.status_code, 404)

    def test_venue_resolved_by_name(self):
        response = self.book(venueId=None, venueName='Grand Palace Hall')
        self.assertEqual(response.status_code, 201)

    def test_anonymous_401(self):
        self.client.force_authenticate(user=None)
        response = self.book()
        self.assertEqual(response.status_code, 401)

    def test_string_numbers_coerced(self):
        response = self.book(amount='920')
        self.assertEqual(response.status_code, 201)


class MyBookingsTests(BookingTestBase):
    def test_lists_only_my_bookings(self):
        self.book()
        other = User.objects.create_user(phone='9000000003', name='Other', email='o@example.com')
        Booking.objects.create(
            listing=self.listing, user=other, date=today_ist(),
            slots=['10:00 – 11:00'], amount=620,
        )
        response = self.client.get('/api/users/me/bookings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 1)
        self.assertEqual(response.data['bookings'][0]['customer'], 'Asha')

    def test_status_filter(self):
        past = today_ist() - datetime.timedelta(days=3)
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=past,
            slots=['10:00 – 11:00'], amount=620,
        )
        self.book()  # upcoming
        upcoming = self.client.get('/api/users/me/bookings?status=upcoming')
        past_resp = self.client.get('/api/users/me/bookings?status=past')
        self.assertEqual(upcoming.data['total'], 1)
        self.assertEqual(past_resp.data['total'], 1)

    def test_bad_status_rejected(self):
        response = self.client.get('/api/users/me/bookings?status=weird')
        self.assertEqual(response.status_code, 400)


class CancelBookingTests(BookingTestBase):
    def test_cancel_frees_the_slots(self):
        booking_id = self.book().data['booking']['id']
        response = self.client.delete(f'/api/users/me/bookings/{booking_id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'cancelled': True, 'id': booking_id})
        # The slot is bookable again:
        self.assertEqual(self.book().status_code, 201)

    def test_cannot_cancel_someone_elses_booking(self):
        booking_id = self.book().data['booking']['id']
        other = User.objects.create_user(phone='9000000004', name='X', email='x@example.com')
        self.client.force_authenticate(user=other)
        response = self.client.delete(f'/api/users/me/bookings/{booking_id}')
        self.assertEqual(response.status_code, 404)

    def test_cannot_cancel_past_booking(self):
        past = today_ist() - datetime.timedelta(days=1)
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=past,
            slots=['10:00 – 11:00'], amount=620,
        )
        response = self.client.delete(f'/api/users/me/bookings/{booking.id}')
        self.assertEqual(response.status_code, 400)


class WalkInBookingTests(BookingTestBase):
    def walk_in(self, **overrides):
        self.client.force_authenticate(user=self.vendor)
        body = {
            'venueName': 'Grand Palace Hall',
            'date': TOMORROW,
            'slots': ['21:30 – 23:30'],  # 2h
            'customer': 'Walk-in Ramesh',
            'perSlot': 600,
            'amount': 1200,              # 2h x 600, NO ₹20 fee
            **overrides,
        }
        return self.client.post('/api/vendors/me/walkin-bookings', body, format='json')

    def test_happy_path(self):
        response = self.walk_in()
        self.assertEqual(response.status_code, 201)
        booking = response.data['booking']
        self.assertTrue(booking['walkIn'])
        self.assertEqual(booking['method'], 'walk-in')
        self.assertIsNone(booking['phone'])
        self.assertEqual(booking['amount'], 1200)
        self.assertEqual(booking['customer'], 'Walk-in Ramesh')

    def test_amount_mismatch_rejected(self):
        response = self.walk_in(amount=500)
        self.assertEqual(response.status_code, 400)

    def test_conflicts_with_customer_booking(self):
        self.book(slots=['21:30 – 23:30'], amount=1220)  # customer books first
        response = self.walk_in()
        self.assertEqual(response.status_code, 409)

    def test_blocks_customer_bookings_too(self):
        self.walk_in()
        response = self.book(slots=['21:30 – 23:30'], amount=1220)
        self.assertEqual(response.status_code, 409)

    def test_cannot_book_foreign_venue(self):
        other_vendor = User.objects.create_user(
            phone='9000000005', name='V2', email='v2@example.com',
            role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=other_vendor)
        body = {
            'venueName': 'Grand Palace Hall', 'date': TOMORROW,
            'slots': ['10:00 – 11:00'], 'perSlot': 600, 'amount': 600,
        }
        response = self.client.post('/api/vendors/me/walkin-bookings', body, format='json')
        self.assertEqual(response.status_code, 404)

    def test_customer_role_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.post('/api/vendors/me/walkin-bookings', {}, format='json')
        self.assertEqual(response.status_code, 403)


class VendorDashboardTests(BookingTestBase):
    def get_dashboard(self):
        self.client.force_authenticate(user=self.vendor)
        return self.client.get('/api/vendors/me/dashboard')

    def seed_bookings(self):
        today = today_ist()
        # Today: one online (₹920), one walk-in (₹1200).
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=today,
            slots=['19:30 – 21:00'], amount=920, customer_name='Asha',
            venue_name='Grand Palace Hall',
        )
        Booking.objects.create(
            listing=self.listing, user=None, date=today,
            slots=['10:00 – 12:00'], amount=1200, customer_name='Ramesh',
            venue_name='Grand Palace Hall',
            method=Booking.Method.WALK_IN, walk_in=True,
        )
        # 3 days ago (in this week): online ₹500.
        Booking.objects.create(
            listing=self.listing, user=self.customer,
            date=today - datetime.timedelta(days=3),
            slots=['10:00 – 11:00'], amount=500,
        )
        # 20 days ago (this month, not this week): ₹1000.
        Booking.objects.create(
            listing=self.listing, user=self.customer,
            date=today - datetime.timedelta(days=20),
            slots=['10:00 – 11:00'], amount=1000,
        )

    def test_stats_and_earnings_split(self):
        self.seed_bookings()
        response = self.get_dashboard()
        self.assertEqual(response.status_code, 200)

        stats = response.data['stats']
        self.assertEqual(stats['today']['value'], 2120)       # 920 + 1200
        self.assertEqual(stats['slotsToday']['value'], 2)
        self.assertEqual(stats['week']['value'], 2620)        # + 500
        self.assertEqual(stats['month']['value'], 3620)       # + 1000

        earnings = response.data['earnings']
        self.assertEqual(earnings['walkIn']['today'], 1200)
        self.assertEqual(earnings['online']['today'], 920)
        self.assertEqual(earnings['total']['month'], 3620)

    def test_week_chart_has_7_days(self):
        self.seed_bookings()
        week = self.get_dashboard().data['week']
        self.assertEqual(len(week), 7)
        self.assertEqual(week[-1]['value'], 2120)  # last entry = today
        self.assertEqual(week[-1]['online'], 920)
        self.assertEqual(week[-1]['walkIn'], 1200)

    def test_today_bookings_and_all_bookings(self):
        self.seed_bookings()
        data = self.get_dashboard().data
        self.assertEqual(len(data['bookings']), 2)  # today only
        self.assertEqual(data['bookings'][0]['venue'], 'Grand Palace Hall')
        self.assertEqual(len(data['allBookings']), 4)

    def test_scoped_to_own_venues_only(self):
        self.seed_bookings()
        other_vendor = User.objects.create_user(
            phone='9000000006', name='V3', email='v3@example.com',
            role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=other_vendor)
        response = self.client.get('/api/vendors/me/dashboard')
        self.assertEqual(response.data['stats']['today']['value'], 0)
        self.assertEqual(response.data['allBookings'], [])
        self.assertEqual(response.data['venues'], [])

    def test_venues_listed(self):
        venues = self.get_dashboard().data['venues']
        self.assertEqual(len(venues), 1)
        self.assertEqual(venues[0]['name'], 'Grand Palace Hall')

    def test_customer_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.get('/api/vendors/me/dashboard')
        self.assertEqual(response.status_code, 403)


class DeleteListingTests(BookingTestBase):
    def delete_listing(self, listing_id=None):
        self.client.force_authenticate(user=self.vendor)
        return self.client.delete(
            f'/api/vendors/me/listings/{listing_id or self.listing.id}'
        )

    def test_delete_without_bookings(self):
        response = self.delete_listing()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['deleted'], True)
        self.assertFalse(Listing.objects.filter(pk=self.listing.id).exists())

    def test_blocked_by_upcoming_bookings(self):
        self.book()
        response = self.delete_listing()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Listing.objects.filter(pk=self.listing.id).exists())

    def test_past_bookings_survive_deletion(self):
        past = today_ist() - datetime.timedelta(days=5)
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=past,
            slots=['10:00 – 11:00'], amount=620, venue_name='Grand Palace Hall',
        )
        self.delete_listing()
        booking.refresh_from_db()
        self.assertIsNone(booking.listing)  # link cleared...
        self.assertEqual(booking.venue_name, 'Grand Palace Hall')  # ...history kept

    def test_foreign_listing_404(self):
        other_vendor = User.objects.create_user(
            phone='9000000007', name='V4', email='v4@example.com',
            role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=other_vendor)
        response = self.client.delete(f'/api/vendors/me/listings/{self.listing.id}')
        self.assertEqual(response.status_code, 404)


class AvailabilityTests(BookingTestBase):
    def test_empty_day(self):
        response = self.client.get(
            f'/api/venues/{self.listing.id}/availability?date={TOMORROW}'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'date': TOMORROW, 'booked': []})

    def test_shows_booked_ranges_sorted(self):
        self.book(slots=['21:30 – 23:30'], amount=1220)
        self.book(slots=['19:30 – 21:00'], amount=920)
        self.client.force_authenticate(user=None)  # public endpoint
        response = self.client.get(
            f'/api/venues/{self.listing.id}/availability?date={TOMORROW}'
        )
        self.assertEqual(
            response.data['booked'], ['19:30 – 21:00', '21:30 – 23:30']
        )

    def test_missing_or_bad_date(self):
        response = self.client.get(f'/api/venues/{self.listing.id}/availability')
        self.assertEqual(response.status_code, 400)
        response = self.client.get(
            f'/api/venues/{self.listing.id}/availability?date=21-07-2026'
        )
        self.assertEqual(response.status_code, 400)

    def test_past_date_rejected(self):
        yesterday = (today_ist() - datetime.timedelta(days=1)).isoformat()
        response = self.client.get(
            f'/api/venues/{self.listing.id}/availability?date={yesterday}'
        )
        self.assertEqual(response.status_code, 400)

    def test_unknown_venue_404(self):
        response = self.client.get(
            f'/api/venues/{uuid.uuid4()}/availability?date={TOMORROW}'
        )
        self.assertEqual(response.status_code, 404)
