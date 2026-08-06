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
            {'name': 'Water bottle 1L', 'price': 30},
        ],
        'packages': [{'label': 'Birthday Deluxe', 'price': 5000}],
        'extraPersonPrice': '200',
        'maxExtraPersons': '4',
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
        # The frontend sends each add-on's price; the server sums them.
        response = self.book(
            addons=[
                {'name': 'Photographer', 'qty': 1, 'price': 2000},
                {'name': 'Cake', 'qty': 2, 'price': 500},
            ],
            amount=920 + 2000 + 1000,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['amount'], 3920)
        # Server echoes the add-on lines from the request:
        self.assertEqual(response.data['booking']['addons'][0]['price'], 2000)

    def test_wrong_amount_rejected(self):
        response = self.book(amount=100)  # trying to underpay
        self.assertEqual(response.status_code, 400)
        self.assertIn('Amount mismatch', response.data['message'])

    def test_amount_mismatch_is_structured(self):
        # P5a: frontend reads code + expectedAmount without regexing the text.
        response = self.book(amount=100)
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(response.data['expectedAmount'], 920)

    def test_custom_and_package_addons_accepted(self):
        # P1: packages, extra-persons and custom add-ons are folded into the
        # addons array as priced line items. The server must accept them ALL
        # (never reject an unrecognised name) and sum the request prices.
        response = self.book(
            addons=[
                {'name': 'Water bottle 1L', 'qty': 4, 'price': 30},    # custom add-on
                {'name': 'Birthday Deluxe', 'qty': 1, 'price': 5000},  # a package
                {'name': 'Extra persons', 'qty': 4, 'price': 200},     # extra-person line
            ],
            # 900 slot + (120 + 5000 + 800) add-ons + 20 fee
            amount=6840,
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['amount'], 6840)
        self.assertEqual(len(response.data['booking']['addons']), 3)

    def test_tampered_addon_price_rejected(self):
        # LIVE money: the catalogue price (₹2000) wins over the client's ₹1 —
        # the total no longer matches and the structured mismatch fires, so
        # honest clients auto-retry with the server number.
        response = self.book(
            addons=[{'name': 'Photographer', 'qty': 1, 'price': 1}],
            amount=920 + 1,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(response.data['expectedAmount'], 920 + 2000)

    def test_unknown_addon_rejected(self):
        response = self.book(
            addons=[{'name': 'Helicopter ride', 'qty': 1, 'price': 50}],
            amount=970,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('Unknown add-on', response.data['message'])

    def test_extra_person_cap_enforced(self):
        # maxExtraPersons is 4 — 5 must be rejected.
        response = self.book(
            addons=[{'name': 'Extra person', 'qty': 5, 'price': 200}],
            amount=920 + 1000,
        )
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

    def test_pay_at_venue_method_stored_and_echoed(self):
        response = self.book(method='venue')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['method'], 'venue')
        self.assertFalse(response.data['booking']['walkIn'])
        # Amount is identical to online — vendor collects it all on arrival.
        self.assertEqual(response.data['booking']['amount'], 920)

    def test_default_method_is_online(self):
        response = self.book()
        self.assertEqual(response.data['booking']['method'], 'online')

    def test_bogus_method_rejected(self):
        response = self.book(method='crypto')
        self.assertEqual(response.status_code, 400)

    def test_method_echoed_verbatim_everywhere(self):
        # upi booking + venue booking -> all three read paths echo them raw.
        self.book(method='upi')
        self.book(method='venue', slots=['21:00 – 22:00'], amount=620)

        mine = self.client.get('/api/users/me/bookings').data['bookings']
        self.assertEqual({b['method'] for b in mine}, {'upi', 'venue'})

        self.client.force_authenticate(user=self.vendor)
        dash = self.client.get('/api/vendors/me/dashboard').data['allBookings']
        self.assertEqual({b['method'] for b in dash}, {'upi', 'venue'})

        admin = User.objects.create_user(
            phone='9000000099', name='Admin', email='adm@example.com',
            role=User.Role.ADMIN, password='AdminPass123',
        )
        self.client.force_authenticate(user=admin)
        rows = self.client.get('/api/admin/bootstrap').data['bookings']
        self.assertEqual({b['method'] for b in rows}, {'upi', 'venue'})

    def test_pay_at_venue_blocks_slots(self):
        self.book(method='venue')
        response = self.book(slots=['20:00 – 22:00'], amount=1220)
        self.assertEqual(response.status_code, 409)  # overlap sees it

    def test_vendor_paused_venue_rejects_customer_bookings(self):
        # Vendor pauses via the record flag (frontend republish) — customers
        # who already had the flow open get a clean 409.
        record = {**self.listing.record, 'paused': True, 'pauseReason': 'Rain'}
        Listing.objects.filter(pk=self.listing.pk).update(record=record)
        response = self.book()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data['message'],
            'This venue is temporarily not accepting bookings',
        )

    def test_detail_paused_flag_also_rejects(self):
        record = dict(self.listing.record)
        record['detail'] = {**(record.get('detail') or {}), 'paused': True}
        Listing.objects.filter(pk=self.listing.pk).update(record=record)
        self.assertEqual(self.book().status_code, 409)

    def test_walkins_still_allowed_on_vendor_paused_venue(self):
        # Pausing blocks CUSTOMERS, not the vendor's own walk-in entries.
        record = {**self.listing.record, 'paused': True, 'pauseReason': 'Rain'}
        Listing.objects.filter(pk=self.listing.pk).update(record=record)
        self.client.force_authenticate(user=self.vendor)
        response = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': ['10:00 – 11:00'], 'customer': 'W',
            'perSlot': 600, 'amount': 600,
        }, format='json')
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

    def test_cannot_cancel_same_day_booking_after_it_ended(self):
        # 06:00–08:00 today; the clock says 09:00 -> ended, cancel refused.
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['06:00 – 08:00'], amount=1220,
        )
        from unittest.mock import patch
        with patch('bookings.views.now_minutes_ist', return_value=9 * 60):
            response = self.client.delete(f'/api/users/me/bookings/{booking.id}')
        self.assertEqual(response.status_code, 400)

    def test_can_cancel_same_day_booking_before_it_ends(self):
        # Same booking, but the clock says 07:00 -> still running, cancel OK.
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['06:00 – 08:00'], amount=1220,
        )
        from unittest.mock import patch
        with patch('bookings.views.now_minutes_ist', return_value=7 * 60):
            response = self.client.delete(f'/api/users/me/bookings/{booking.id}')
        self.assertEqual(response.status_code, 200)


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
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')  # P5a

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
        self.assertEqual(response.data['date'], TOMORROW)
        self.assertEqual(response.data['booked'], [])
        self.assertEqual(response.data['bookedUnits'], [])

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

    def test_unknown_venue_404_avail(self):
        response = self.client.get(
            f'/api/venues/{uuid.uuid4()}/availability?date={TOMORROW}'
        )
        self.assertEqual(response.status_code, 404)

    def test_malformed_id_returns_json_404(self):
        # P5b: a non-UUID id matches no route -> JSON, not Django's HTML page.
        response = self.client.get(f'/api/venues/not-a-uuid/availability?date={TOMORROW}')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()['message'], 'Not found')


# A playzone venue with two box-cricket pitches at DIFFERENT hourly rates.
PLAYZONE_RECORD = {
    'name': 'Turf Arena',
    'category': 'Box cricket',
    'location': 'HSR, Bengaluru',
    'price': 599,
    'image': 'https://cdn.example/turf.jpg',
    'gallery': [],
    'detail': {
        'sports': [
            {'name': 'Box Cricket', 'price': '599', 'units': 2,
             'unitPrices': ['599', '1999']},
        ],
        'addons': [],
    },
}


OFFER_RECORD = {
    'name': 'Offer Hall', 'category': 'hall', 'location': 'x',
    'price': 600, 'image': '', 'gallery': [],
    'detail': {
        'addons': [],
        'offers': [
            {'title': 'Weekend', 'code': 'SAVE10', 'type': 'percent',
             'value': '10', 'minAmount': '500', 'maxDiscount': '300', 'expiry': ''},
            {'title': 'Flat', 'code': 'FLAT100', 'type': 'flat',
             'value': '100', 'minAmount': '', 'maxDiscount': '', 'expiry': ''},
            {'title': 'Auto', 'code': '', 'type': 'percent',
             'value': '5', 'minAmount': '', 'maxDiscount': '', 'expiry': ''},
            {'title': 'Old', 'code': 'OLD', 'type': 'percent',
             'value': '50', 'minAmount': '', 'maxDiscount': '', 'expiry': '2020-01-01'},
        ],
    },
}


class OfferBookingTests(BookingTestBase):
    """Coupon/offer discounts applied at booking time (item 4)."""

    def setUp(self):
        super().setUp()
        self.offer_listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='offer-hall',
            record={**OFFER_RECORD, 'id': 'o', 'status': 'live'},
            name='Offer Hall', category='hall', locality='x', pincode='560001',
        )
        self.client.force_authenticate(user=self.customer)

    def book(self, offer=None, **overrides):
        body = {
            'venueId': str(self.offer_listing.id),
            'date': TOMORROW,
            'slots': ['19:00 – 21:00'],   # 2h x 600 = 1200 base
            'addons': [],
            'perSlot': 600,
            'offer': offer,
            **overrides,
        }
        return self.client.post('/api/users/me/bookings', body, format='json')

    def test_percent_offer(self):
        # 1200 - 10% (120) + 20 fee = 1100
        r = self.book(offer={'code': 'SAVE10'}, amount=1100)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 120)
        self.assertEqual(r.data['booking']['amount'], 1100)
        self.assertEqual(r.data['booking']['offer']['code'], 'SAVE10')

    def test_percent_offer_capped_by_maxDiscount(self):
        # 6h x 600 = 3600; 10% = 360 but capped at 300; 3300 + 20 = 3320
        r = self.book(slots=['12:00 – 18:00'], offer={'code': 'SAVE10'}, amount=3320)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 300)

    def test_flat_offer(self):
        # 1200 - 100 + 20 = 1120
        r = self.book(offer={'code': 'FLAT100'}, amount=1120)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 100)

    def test_blank_code_auto_applies(self):
        # Auto offer: 5% of 1200 = 60; 1140 + 20 = 1160
        r = self.book(offer={'code': ''}, amount=1160)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 60)

    def test_no_offer_means_no_discount(self):
        r = self.book(offer=None, amount=1220)  # 1200 + 20
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 0)
        self.assertIsNone(r.data['booking']['offer'])

    def test_min_amount_rejected(self):
        # 30 min x 600 = 300 base, below SAVE10's ₹500 minimum
        r = self.book(slots=['19:00 – 19:30'], offer={'code': 'SAVE10'}, amount=300)
        self.assertEqual(r.status_code, 400)

    def test_expired_offer_rejected(self):
        r = self.book(offer={'code': 'OLD'}, amount=620)
        self.assertEqual(r.status_code, 400)

    def test_unknown_offer_rejected(self):
        r = self.book(offer={'code': 'NOPE'}, amount=1220)
        self.assertEqual(r.status_code, 400)

    def test_fee_is_not_discounted(self):
        # discount 120 applies to base only; fee stays ₹20 (1200-120+20=1100)
        r = self.book(offer={'code': 'SAVE10'}, amount=1100)
        self.assertEqual(r.data['booking']['amount'] - (1200 - 120), 20)

    def test_client_discount_amount_cross_checked(self):
        # SAVE10 on 1200 base -> our discount is 120. A client claiming 200
        # (drift > ₹1) is rejected with the structured body (silent retry).
        r = self.book(offer={'code': 'SAVE10'}, discountAmount=200, amount=1100)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            r.data['message'], 'Offer amount mismatch — please retry the booking'
        )
        self.assertEqual(r.data['code'], 'OFFER_MISMATCH')
        self.assertEqual(r.data['discountAmount'], 120)  # server's recompute

    def test_client_discount_within_one_rupee_tolerated(self):
        r = self.book(offer={'code': 'SAVE10'}, discountAmount=119, amount=1100)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 120)  # ours wins

    def test_matched_offer_with_zero_discount_rejected(self):
        record = dict(self.offer_listing.record)
        record['detail'] = {
            **record['detail'],
            'offers': record['detail']['offers'] + [
                {'title': 'Dud', 'code': 'ZERO', 'type': 'percent',
                 'value': '0', 'minAmount': '', 'maxDiscount': '', 'expiry': ''},
            ],
        }
        Listing.objects.filter(pk=self.offer_listing.pk).update(record=record)
        r = self.book(offer={'code': 'ZERO'}, amount=1220)
        self.assertEqual(r.status_code, 400)

    def test_walkin_applies_offer_without_fee(self):
        self.client.force_authenticate(user=self.vendor)
        body = {
            'venueId': str(self.offer_listing.id), 'date': TOMORROW,
            'slots': ['19:00 – 21:00'], 'customer': 'Walk-in',
            'perSlot': 600, 'offer': {'code': 'FLAT100'}, 'amount': 1100,  # 1200-100, no fee
        }
        r = self.client.post('/api/vendors/me/walkin-bookings', body, format='json')
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 100)
        self.assertEqual(r.data['booking']['amount'], 1100)


from django.test import override_settings


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key',
    RAZORPAY_KEY_SECRET='test_key_secret',
    RAZORPAY_WEBHOOK_SECRET='test_webhook_secret',
)
class PaymentFlowTests(BookingTestBase):
    """Razorpay order -> verify / webhook -> confirm. Gateway HTTP mocked;
    signatures are REAL HMACs computed with the test secrets."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()  # throttle counters persist in the cache between tests

    def order(self, **overrides):
        from unittest.mock import MagicMock, patch
        body = {
            'venueId': str(self.listing.id), 'date': TOMORROW,
            'slots': ['19:30 – 21:00'], 'addons': [], 'perSlot': 600,
            'amount': 920,
            **overrides,
        }
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': 'order_TEST123'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            return self.client.post('/api/payments/order', body, format='json')

    def _sign(self, order_id, payment_id):
        import hashlib, hmac
        return hmac.new(
            b'test_key_secret', f'{order_id}|{payment_id}'.encode(), hashlib.sha256
        ).hexdigest()

    def test_order_creates_pending_hold(self):
        response = self.order()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['orderId'], 'order_TEST123')
        self.assertEqual(response.data['keyId'], 'rzp_test_key')
        self.assertEqual(response.data['amount'], 92000)  # paise
        booking = Booking.objects.get(pk=response.data['bookingId'])
        self.assertEqual(booking.status, 'payment_pending')
        # The hold blocks a direct booking of the same slot:
        self.assertEqual(self.book().status_code, 409)
        # ...but is invisible in the customer's bookings list:
        mine = self.client.get('/api/users/me/bookings').data
        self.assertEqual(mine['total'], 0)

    def test_expired_hold_frees_the_slot(self):
        from django.utils import timezone as dj_tz
        response = self.order()
        Booking.objects.filter(pk=response.data['bookingId']).update(
            created_at=dj_tz.now() - datetime.timedelta(minutes=31)
        )
        self.assertEqual(self.book().status_code, 201)  # slot free again

    def test_verify_confirms_with_valid_signature(self):
        order = self.order().data
        response = self.client.post('/api/payments/verify', {
            'bookingId': order['bookingId'],
            'razorpay_order_id': 'order_TEST123',
            'razorpay_payment_id': 'pay_ABC',
            'razorpay_signature': self._sign('order_TEST123', 'pay_ABC'),
        }, format='json')
        self.assertEqual(response.status_code, 200)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.razorpay_payment_id, 'pay_ABC')

    def test_verify_rejects_bad_signature(self):
        order = self.order().data
        response = self.client.post('/api/payments/verify', {
            'bookingId': order['bookingId'],
            'razorpay_order_id': 'order_TEST123',
            'razorpay_payment_id': 'pay_ABC',
            'razorpay_signature': 'forged',
        }, format='json')
        self.assertEqual(response.status_code, 400)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'payment_pending')  # unconfirmed

    def _webhook(self, event, order_id='order_TEST123'):
        import hashlib, hmac, json as json_mod
        payload = json_mod.dumps({
            'event': event,
            'payload': {'payment': {'entity': {'id': 'pay_WH1', 'order_id': order_id}}},
        })
        signature = hmac.new(
            b'test_webhook_secret', payload.encode(), hashlib.sha256
        ).hexdigest()
        return self.client.post(
            '/api/payments/webhook', payload,
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=signature,
        )

    def test_webhook_captured_confirms(self):
        order = self.order().data
        response = self._webhook('payment.captured')
        self.assertEqual(response.status_code, 200)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'confirmed')

    def test_webhook_failed_releases_the_hold(self):
        self.order()
        self._webhook('payment.failed')
        self.assertEqual(self.book(slots=['19:30 – 21:00']).status_code, 201)

    def test_webhook_unhandled_event_acknowledged(self):
        # Dispute/downtime events must get a 200 (Razorpay disables webhooks
        # that keep failing), even though we don't process them.
        self.order()
        response = self._webhook('payment.dispute.created')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'status': 'ignored'})

    def test_webhook_bad_signature_rejected(self):
        self.order()
        response = self.client.post(
            '/api/payments/webhook', '{"event":"payment.captured"}',
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE='forged',
        )
        self.assertEqual(response.status_code, 400)

    def test_dashboard_excludes_abandoned_and_refunded(self):
        # An abandoned checkout must not appear as a booking, nor count in
        # the vendor's stats/earnings — mirroring the availability filter.
        order = self.order().data
        self.client.delete(f"/api/users/me/bookings/{order['bookingId']}")   # -> cancelled
        Booking.objects.create(   # a refunded one too
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['10:00 – 11:00'], amount=5000, fee=20, status='refunded',
            venue_name='Grand Palace Hall',
        )
        paid = Booking.objects.create(  # the only real one
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['12:00 – 13:00'], amount=620, fee=20,
            venue_name='Grand Palace Hall',
        )

        self.client.force_authenticate(user=self.vendor)
        data = self.client.get('/api/vendors/me/dashboard').data
        ids = {b['id'] for b in data['allBookings']}
        self.assertEqual(ids, {paid.id})                      # lists clean
        self.assertEqual(data['stats']['today']['value'], 620)  # earnings clean
        self.assertEqual(data['earnings']['total']['today'], 620)
        self.assertEqual(data['stats']['slotsToday']['value'], 1)

    def test_booking_records_expose_status(self):
        order = self.order().data
        self.client.delete(f"/api/users/me/bookings/{order['bookingId']}")
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.as_record()['status'], 'cancelled')

    def test_customer_can_cancel_unpaid_hold_and_slot_frees(self):
        # Frontend closes the Razorpay widget -> DELETE the pending booking.
        order = self.order().data
        response = self.client.delete(f"/api/users/me/bookings/{order['bookingId']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['cancelled'], True)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'cancelled')  # row KEPT for late webhooks
        self.assertEqual(self.book().status_code, 201)  # slot free immediately

    def test_capture_after_cancel_auto_refunds_and_never_revives(self):
        # UPI-collect edge: customer closes the widget, then approves in their
        # UPI app -> money captured for a cancelled booking.
        from unittest.mock import MagicMock, patch
        order = self.order().data
        self.client.delete(f"/api/users/me/bookings/{order['bookingId']}")

        refund_ok = MagicMock(status_code=200)
        refund_ok.json.return_value = {'id': 'rfnd_AUTO'}
        no_existing = MagicMock(status_code=200)
        no_existing.json.return_value = {'items': []}
        with patch('bookings.razorpay_client.requests.post', return_value=refund_ok), \
             patch('bookings.razorpay_client.requests.get', return_value=no_existing):
            response = self._webhook('payment.captured')
        self.assertEqual(response.status_code, 200)

        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'refunded')      # NOT resurrected
        self.assertEqual(booking.refund_id, 'rfnd_AUTO')  # money returned
        self.assertEqual(booking.refund_amount, booking.amount)

    def test_auto_refund_failure_queues_for_manual_refund(self):
        from unittest.mock import MagicMock, patch
        order = self.order().data
        self.client.delete(f"/api/users/me/bookings/{order['bookingId']}")

        broken = MagicMock(status_code=500)
        no_existing = MagicMock(status_code=200)
        no_existing.json.return_value = {'items': []}
        with patch('bookings.razorpay_client.requests.post', return_value=broken), \
             patch('bookings.razorpay_client.requests.get', return_value=no_existing):
            self._webhook('payment.captured')

        booking = Booking.objects.get(pk=order['bookingId'])
        # Money is never silently kept — the admin Refunds panel picks it up.
        self.assertEqual(booking.status, 'refund_pending')
        self.assertEqual(booking.razorpay_payment_id, 'pay_WH1')

    def test_replayed_signature_cannot_revive_refunded_booking(self):
        # CRITICAL fix: the customer holds a valid signature from their own
        # checkout — replaying it after a refund must NOT re-confirm.
        order = self.order().data
        good = {
            'bookingId': order['bookingId'],
            'razorpay_order_id': 'order_TEST123',
            'razorpay_payment_id': 'pay_ABC',
            'razorpay_signature': self._sign('order_TEST123', 'pay_ABC'),
        }
        self.client.post('/api/payments/verify', good, format='json')  # confirm
        Booking.objects.filter(pk=order['bookingId']).update(status='refunded')
        replay = self.client.post('/api/payments/verify', good, format='json')
        self.assertEqual(replay.status_code, 409)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'refunded')  # NOT revived

    def test_late_webhook_capture_after_resale_goes_to_refund_queue(self):
        # CRITICAL fix: hold expires, someone else books the slot, THEN the
        # late payment.captured arrives -> refund_pending, never double-booked.
        from django.utils import timezone as dj_tz
        order = self.order().data
        Booking.objects.filter(pk=order['bookingId']).update(
            created_at=dj_tz.now() - datetime.timedelta(minutes=31)
        )
        self.assertEqual(self.book().status_code, 201)  # slot re-sold
        response = self._webhook('payment.captured')
        self.assertEqual(response.status_code, 200)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'refund_pending')  # queued, not confirmed

    def test_webhook_amount_mismatch_never_confirms(self):
        import hashlib, hmac, json as json_mod
        order = self.order().data
        payload = json_mod.dumps({
            'event': 'payment.captured',
            'payload': {'payment': {'entity': {
                'id': 'pay_WH1', 'order_id': 'order_TEST123', 'amount': 100,
            }}},
        })
        signature = hmac.new(
            b'test_webhook_secret', payload.encode(), hashlib.sha256
        ).hexdigest()
        response = self.client.post(
            '/api/payments/webhook', payload,
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 200)
        booking = Booking.objects.get(pk=order['bookingId'])
        self.assertEqual(booking.status, 'payment_pending')  # untouched

    def test_refund_reuses_existing_gateway_refund(self):
        # Idempotency: a refund already exists at the gateway (lost response
        # last time) -> reuse it, never refund twice.
        from unittest.mock import MagicMock, patch
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=TOMORROW,
            slots=['12:00 – 13:00'], amount=620, fee=20,
            razorpay_order_id='order_Y', razorpay_payment_id='pay_Y',
        )
        admin = User.objects.create_user(
            phone='9000000097', name='Admin2', email='ad3@example.com',
            role=User.Role.ADMIN, password='AdminPass123',
        )
        self.client.force_authenticate(user=admin)
        existing = MagicMock(status_code=200)
        existing.json.return_value = {'items': [{'id': 'rfnd_OLD'}]}
        with patch('bookings.razorpay_client.requests.get', return_value=existing), \
             patch('bookings.razorpay_client.requests.post') as mock_post:
            response = self.client.patch(
                f'/api/admin/bookings/{booking.id}', {'status': 'refunded'},
                format='json',
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.refund_id, 'rfnd_OLD')
        mock_post.assert_not_called()  # no second gateway refund

    def test_admin_refund_calls_gateway_and_stores_id(self):
        from unittest.mock import MagicMock, patch
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=TOMORROW,
            slots=['10:00 – 11:00'], amount=620, fee=20,
            razorpay_order_id='order_X', razorpay_payment_id='pay_X',
        )
        admin = User.objects.create_user(
            phone='9000000098', name='Admin', email='ad2@example.com',
            role=User.Role.ADMIN, password='AdminPass123',
        )
        self.client.force_authenticate(user=admin)
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': 'rfnd_001'}
        no_existing = MagicMock(status_code=200)
        no_existing.json.return_value = {'items': []}
        with patch('bookings.razorpay_client.requests.post', return_value=fake) as mock_post, \
             patch('bookings.razorpay_client.requests.get', return_value=no_existing):
            response = self.client.patch(
                f'/api/admin/bookings/{booking.id}',
                {'status': 'refunded', 'refundAmount': 300}, format='json',
            )
        self.assertEqual(response.status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refunded')
        self.assertEqual(booking.refund_id, 'rfnd_001')
        self.assertIn('/payments/pay_X/refund', mock_post.call_args[0][0])
        self.assertEqual(mock_post.call_args[1]['json'], {'amount': 30000})  # paise


class RatingTests(BookingTestBase):
    """POST /api/venues/:venueId/ratings — one rating per completed booking."""

    def setUp(self):
        super().setUp()
        yesterday = today_ist() - datetime.timedelta(days=1)
        self.done = Booking.objects.create(
            listing=self.listing, user=self.customer, date=yesterday,
            slots=['10:00 – 11:00'], amount=620, venue_name='Grand Palace Hall',
        )

    def rate(self, stars=4, booking_id=None, venue_id=None):
        return self.client.post(
            f'/api/venues/{venue_id or self.listing.id}/ratings',
            {'bookingId': booking_id or self.done.id, 'stars': stars},
            format='json',
        )

    def test_happy_path_returns_aggregate(self):
        response = self.rate(stars=4)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, {'rating': 4.0, 'count': 1})

    def test_average_over_multiple_bookings(self):
        self.rate(stars=4)
        other = Booking.objects.create(
            listing=self.listing, user=self.customer,
            date=today_ist() - datetime.timedelta(days=2),
            slots=['12:00 – 13:00'], amount=620,
        )
        response = self.rate(stars=5, booking_id=other.id)
        self.assertEqual(response.data, {'rating': 4.5, 'count': 2})

    def test_bad_stars_rejected(self):
        for stars in (0, 6, 'abc', None):
            self.assertEqual(self.rate(stars=stars).status_code, 400)

    def test_unknown_booking_404(self):
        self.assertEqual(self.rate(booking_id='bk_nope').status_code, 404)

    def test_foreign_booking_403(self):
        other = User.objects.create_user(phone='9000000033', name='O', email='o2@example.com')
        self.client.force_authenticate(user=other)
        self.assertEqual(self.rate().status_code, 403)

    def test_wrong_venue_400(self):
        other_listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='other-hall',
            record={'name': 'Other', 'price': 500, 'detail': {}},
            name='Other', category='hall', locality='x', pincode='560001',
        )
        self.assertEqual(self.rate(venue_id=other_listing.id).status_code, 400)

    def test_unit_sibling_counts_as_same_venue(self):
        # Booking made on a unit sibling; rating posted to the base id -> OK,
        # and both share one aggregate.
        sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='gph-hall-2',
            record={'name': 'Grand Palace Hall — Hall 2', 'price': 600,
                    'detail': {'unitOf': str(self.listing.id)}},
            name='Grand Palace Hall — Hall 2', category='hall',
            locality='x', pincode='560001',
        )
        on_sibling = Booking.objects.create(
            listing=sibling, user=self.customer,
            date=today_ist() - datetime.timedelta(days=1),
            slots=['12:00 – 13:00'], amount=620,
        )
        response = self.rate(stars=5, booking_id=on_sibling.id)  # posted to BASE id
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['count'], 1)

    def test_upcoming_booking_409(self):
        upcoming = Booking.objects.create(
            listing=self.listing, user=self.customer,
            date=today_ist() + datetime.timedelta(days=1),
            slots=['10:00 – 11:00'], amount=620,
        )
        response = self.rate(booking_id=upcoming.id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['message'], 'Booking not completed yet')

    def test_double_rating_409(self):
        self.rate(stars=4)
        response = self.rate(stars=5)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['message'], 'Already rated')
        # The average is untouched by the double-submit:
        from bookings.ratings import venue_rating
        self.assertEqual(venue_rating(self.listing), (4.0, 1))

    def test_anonymous_401(self):
        self.client.force_authenticate(user=None)
        self.assertEqual(self.rate().status_code, 401)

    def test_dashboard_venues_carry_rating_even_when_paused(self):
        # Item 1 of the consolidated round: the vendor sees the aggregate on
        # their own dashboard regardless of venue status.
        self.rate(stars=4)
        Listing.objects.filter(pk=self.listing.pk).update(status='paused')
        self.client.force_authenticate(user=self.vendor)
        venues = self.client.get('/api/vendors/me/dashboard').data['venues']
        row = next(v for v in venues if v['id'] == str(self.listing.id))
        self.assertEqual(row['rating'], 4.0)
        self.assertEqual(row['ratingCount'], 1)

    def test_vendor_ratings_feed(self):
        # Two ratings: one on the base (by 'Asha'), one on a sibling with a
        # unit label (by a two-word name -> display-safe truncation).
        self.rate(stars=4)
        long_name = User.objects.create_user(
            phone='9000000044', name='Ravi Kumar', email='rk@example.com',
        )
        sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='gph-screen-2',
            record={'name': 'Grand Palace Hall — Screen 2', 'price': 600,
                    'detail': {'unitOf': str(self.listing.id)}},
            name='Grand Palace Hall — Screen 2', category='hall',
            locality='x', pincode='560001',
        )
        sib_booking = Booking.objects.create(
            listing=sibling, user=long_name,
            date=today_ist() - datetime.timedelta(days=2),
            slots=['12:00 – 13:00'], amount=620, unit_label='Screen 2',
        )
        self.client.force_authenticate(user=long_name)
        self.client.post(
            f'/api/venues/{sibling.id}/ratings',
            {'bookingId': sib_booking.id, 'stars': 5}, format='json',
        )

        self.client.force_authenticate(user=self.vendor)
        data = self.client.get('/api/vendors/me/ratings').data
        self.assertEqual(data['total'], 2)
        newest = data['ratings'][0]  # newest first = the sibling rating
        self.assertEqual(newest['stars'], 5)
        self.assertEqual(newest['unitLabel'], 'Screen 2')
        self.assertEqual(newest['customerName'], 'Ravi K.')  # never phone/email
        self.assertEqual(newest['bookingDate'], sib_booking.date.isoformat())
        base_row = data['ratings'][1]
        self.assertEqual(base_row['customerName'], 'Asha')
        self.assertIsNone(base_row['unitLabel'])

        # Pagination honoured:
        paged = self.client.get('/api/vendors/me/ratings?limit=1&offset=1').data
        self.assertEqual(paged['total'], 2)
        self.assertEqual(len(paged['ratings']), 1)
        self.assertEqual(paged['ratings'][0]['stars'], 4)

        # venueId filter covers the whole venue set (base id -> sibling incl.):
        filtered = self.client.get(
            f'/api/vendors/me/ratings?venueId={self.listing.id}'
        ).data
        self.assertEqual(filtered['total'], 2)

    def test_vendor_ratings_feed_scoped_and_guarded(self):
        self.rate(stars=4)
        other_vendor = User.objects.create_user(
            phone='9000000055', name='V2', email='v2b@example.com',
            role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=other_vendor)
        self.assertEqual(  # someone else's ratings never leak
            self.client.get('/api/vendors/me/ratings').data['total'], 0
        )
        self.assertEqual(  # foreign venueId -> 404
            self.client.get(
                f'/api/vendors/me/ratings?venueId={self.listing.id}'
            ).status_code, 404
        )
        self.client.force_authenticate(user=self.customer)
        self.assertEqual(  # customers 403
            self.client.get('/api/vendors/me/ratings').status_code, 403
        )

    def test_rating_surfaces_on_public_venue(self):
        self.rate(stars=4)
        from django.core.cache import cache
        cache.clear()
        self.client.force_authenticate(user=None)
        detail = self.client.get(f'/api/venues/{self.listing.id}').data
        self.assertEqual(detail['rating'], 4.0)
        self.assertEqual(detail['ratingCount'], 1)
        listing_row = self.client.get('/api/venues').data['venues'][0]
        self.assertEqual(listing_row['rating'], 4.0)


class PlatformPromoTests(BookingTestBase):
    """Banner promo codes (source: 'platform') validated against active banners."""

    def setUp(self):
        super().setUp()
        import datetime as dt
        from adminpanel.models import Settings
        today = today_ist()
        settings_obj = Settings.load()
        settings_obj.banners = [
            {'id': 1, 'title': 'August Fest', 'text': '', 'type': 'percent',
             'value': 15, 'code': 'AUG15', 'minAmount': '500',
             'maxDiscount': '200',
             'from': today.isoformat(), 'to': today.isoformat()},
            {'id': 2, 'title': 'Old promo', 'text': '', 'type': 'flat',
             'value': 100, 'code': 'GONE', 'minAmount': '', 'maxDiscount': '',
             'from': '', 'to': (today - dt.timedelta(days=1)).isoformat()},
        ]
        settings_obj.save()
        self.client.force_authenticate(user=self.customer)

    def book(self, offer=None, **overrides):
        body = {
            'venueId': str(self.listing.id),
            'date': TOMORROW,
            'slots': ['19:00 – 21:00'],  # 2h x 600 = 1200 base
            'addons': [],
            'perSlot': 600,
            'offer': offer,
            **overrides,
        }
        return self.client.post('/api/users/me/bookings', body, format='json')

    def test_platform_percent_promo_applied(self):
        # 1200 - 15% (180, under the 200 cap) + 20 fee = 1040.
        r = self.book(offer={'code': 'AUG15', 'source': 'platform'}, amount=1040)
        self.assertEqual(r.status_code, 201)
        booking = r.data['booking']
        self.assertEqual(booking['discountAmount'], 180)
        self.assertEqual(booking['offer']['source'], 'platform')
        self.assertEqual(booking['offer']['code'], 'AUG15')

    def test_maxDiscount_caps_platform_percent(self):
        # 6h x 600 = 3600; 15% = 540 -> capped at 200; 3400 + 20 = 3420.
        r = self.book(slots=['12:00 – 18:00'],
                      offer={'code': 'AUG15', 'source': 'platform'}, amount=3420)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data['booking']['discountAmount'], 200)

    def test_minAmount_enforced(self):
        # 30 min -> base 300, below the ₹500 minimum.
        r = self.book(slots=['19:00 – 19:30'],
                      offer={'code': 'AUG15', 'source': 'platform'}, amount=300)
        self.assertEqual(r.status_code, 400)

    def test_expired_banner_rejected(self):
        r = self.book(offer={'code': 'GONE', 'source': 'platform'}, amount=1120)
        self.assertEqual(r.status_code, 400)

    def test_unknown_platform_code_rejected(self):
        r = self.book(offer={'code': 'NOPE', 'source': 'platform'}, amount=1220)
        self.assertEqual(r.status_code, 400)

    def test_platform_code_not_confused_with_venue_offers(self):
        # AUG15 is a banner code, not a venue offer — without source: platform
        # it must NOT match (venue catalogue is empty here).
        r = self.book(offer={'code': 'AUG15'}, amount=1040)
        self.assertEqual(r.status_code, 400)


class PerUnitBookingTests(BookingTestBase):
    """P6 — a venue with several pitches/courts/screens (per-unit bookings)."""

    def setUp(self):
        super().setUp()
        self.turf = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='turf-arena',
            record={**PLAYZONE_RECORD, 'id': 'y', 'status': 'live'},
            name='Turf Arena', category='Box cricket',
            locality='HSR', pincode='560102',
        )
        self.client.force_authenticate(user=self.customer)

    def book_unit(self, **overrides):
        body = {
            'venueId': str(self.turf.id),
            'date': TOMORROW,
            'slots': ['19:00 – 21:00'],   # 2h
            'sport': 'Box Cricket',
            'unit': 1,
            'unitLabel': 'Box Cricket · Pitch 1',
            'perSlot': 599,
            'addons': [],
            'amount': 1218,               # 599*2 + ₹20 fee
            **overrides,
        }
        return self.client.post('/api/users/me/bookings', body, format='json')

    def test_two_pitches_bookable_in_same_slot(self):
        first = self.book_unit()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data['booking']['unit'], 1)
        self.assertEqual(first.data['booking']['unitLabel'], 'Box Cricket · Pitch 1')
        # Pitch 2 costs a different rate (₹1999) — same slot, still bookable.
        second = self.book_unit(
            unit=2, perSlot=1999, amount=4018, unitLabel='Box Cricket · Pitch 2'
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data['booking']['amount'], 4018)

    def test_same_pitch_same_slot_conflicts(self):
        self.book_unit()
        self.assertEqual(self.book_unit().status_code, 409)

    def test_wrong_unit_rate_rejected(self):
        # Pitch 2 costs ₹1999 — paying the pitch-1 price must fail.
        response = self.book_unit(unit=2, perSlot=599, amount=1218)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(response.data['expectedAmount'], 4018)

    def test_availability_lists_every_booking(self):
        # A booking carrying sport/unit metadata must STILL appear in the flat
        # `booked` array — each listing is one bookable resource, and the time
        # picker renders exactly this array.
        self.book_unit()  # pitch 1, with sport/unit fields set
        self.client.force_authenticate(user=None)
        data = self.client.get(
            f'/api/venues/{self.turf.id}/availability?date={TOMORROW}'
        ).data
        self.assertEqual(data['booked'], ['19:00 – 21:00'])
        self.assertEqual(len(data['bookedUnits']), 1)
        self.assertEqual(data['bookedUnits'][0]['unit'], 1)
        self.assertEqual(data['bookedUnits'][0]['ranges'], ['19:00 – 21:00'])

    def test_availability_merges_all_units_sorted(self):
        self.book_unit(unit=1, perSlot=599, amount=1218)
        self.book_unit(unit=2, perSlot=1999, amount=4018)  # same slot, pitch 2
        self.book_unit(unit=2, perSlot=1999, amount=2019, slots=['10:00 – 11:00'])
        self.client.force_authenticate(user=None)
        data = self.client.get(
            f'/api/venues/{self.turf.id}/availability?date={TOMORROW}'
        ).data
        self.assertEqual(data['booked'], ['10:00 – 11:00', '19:00 – 21:00'])
        self.assertEqual(len(data['bookedUnits']), 2)

    def test_walkin_with_unit_shows_in_availability(self):
        # Acceptance #2: a walk-in on a unit listing blocks time identically.
        self.client.force_authenticate(user=self.vendor)
        r = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.turf.id), 'date': TOMORROW,
            'slots': ['06:00 – 07:00'], 'customer': 'Walk-in',
            'sport': 'Box Cricket', 'unit': 2, 'unitLabel': 'Pitch 2',
            'perSlot': 600, 'amount': 600,
        }, format='json')
        self.assertEqual(r.status_code, 201)
        self.client.force_authenticate(user=None)
        data = self.client.get(
            f'/api/venues/{self.turf.id}/availability?date={TOMORROW}'
        ).data
        self.assertEqual(data['booked'], ['06:00 – 07:00'])

    def test_legacy_whole_venue_booking_blocks_all_units(self):
        # A booking with no unit (legacy) blocks every pitch (safe default).
        Booking.objects.create(
            listing=self.turf, user=self.customer,
            date=today_ist() + datetime.timedelta(days=1),
            slots=['19:00 – 21:00'], amount=1218,
        )
        self.assertEqual(self.book_unit().status_code, 409)
