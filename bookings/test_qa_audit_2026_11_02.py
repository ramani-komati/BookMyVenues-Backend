"""
Regression tests for the customer-site QA audit of 2026-11-02.

  1. Package pricing paths — one price per booking whatever the path:
     a ₹0 / missing hourly price can't be booked by the hour, a vendor can
     require a package, extra hours only extend a package, "3 hrs" is three
     hours, a package is one continuous block, and the review step's quote
     equals the payment order's amount (weekday, weekend, pitch listings).
  3. Day summary reads the whole window in a constant number of queries.
  6. A venue that never published opening hours is not bookable, and says so.

Only Razorpay's HTTP call is mocked (GatewayBookingMixin); no real booking,
payment or production record is touched — the test database is throwaway.
"""
import datetime
import uuid

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APITestCase

from accounts.models import User
from venues.models import Listing

from .models import Booking
from .slots import today_ist
from .test_security import GatewayBookingMixin
from .tests import OPEN_ALL_DAY, TOMORROW


def next_weekday(weekday):
    today = today_ist()
    return today + datetime.timedelta(days=(weekday - today.weekday()) % 7 or 7)


class Fixtures(GatewayBookingMixin):
    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(phone='9811100001', name='Vendor', role=User.Role.VENDOR)
        self.customer = User.objects.create_user(phone='9811100002', name='Asha')
        self.client.force_authenticate(user=self.customer)

    def venue(self, detail, price=600, slug=None, **record):
        slug = slug or f'qa-{uuid.uuid4().hex[:8]}'
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug=slug,
            record={'name': slug, 'price': price, 'detail': detail, **record},
            name=slug, category='Private theatre', locality='X', pincode='560001',
            status=Listing.Status.LIVE,
        )

    def order(self, listing, slots, addons=(), amount=0, date=TOMORROW, **extra):
        return self.gateway_book({
            'venueId': str(listing.id), 'date': date, 'slots': list(slots),
            'addons': list(addons), 'amount': amount, **extra,
        })

    def quote(self, listing, slots, addons=(), date=TOMORROW, **extra):
        return self.client.post('/api/bookings/quote', {
            'venueId': str(listing.id), 'date': date, 'slots': list(slots),
            'addons': list(addons), **extra,
        }, format='json')


# A theatre registered with packages only — the wizard stored '0' for every
# screen and copied the cheapest package into `price` (live: RK Party House).
PACKAGE_ONLY = {
    'timings': OPEN_ALL_DAY,
    'unitCount': 1,
    'unitPrices': ['0'],
    'packages': [
        {'label': 'Base', 'price': '799', 'duration': '1'},
        {'label': 'Movie Package', 'price': '2499', 'duration': '3'},
    ],
}


@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret')
class PackagePathTests(Fixtures, APITestCase):

    def test_a_zero_unit_price_is_never_a_free_hour(self):
        """REGRESSION: the ₹0 screen price let 3 hours through for the fee."""
        venue = self.venue(PACKAGE_ONLY, price=799)
        r = self.order(venue, ['18:00 – 21:00'], amount=20, unit=1)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(r.data['code'], 'PACKAGE_REQUIRED')
        # …nor at the "from ₹799" card price, which is a package's price.
        r = self.order(venue, ['18:00 – 21:00'], amount=3 * 799 + 20, unit=1)
        self.assertEqual(r.data['code'], 'PACKAGE_REQUIRED')
        self.assertFalse(Booking.objects.exists())

    def test_the_package_path_still_books(self):
        venue = self.venue(PACKAGE_ONLY, price=799)
        r = self.order(venue, ['18:00 – 21:00'], [{'name': 'Package — Movie Package', 'qty': 1}],
                       amount=2499 + 20, unit=1)
        self.assertEqual(r.status_code, 201, r.data)

    def test_vendor_can_require_a_package(self):
        detail = {
            'timings': OPEN_ALL_DAY, 'unitPrices': ['199'], 'packageRequired': True,
            'packages': [{'label': 'Squad', 'price': '499', 'duration': '1'}],
        }
        venue = self.venue(detail, price=199)
        r = self.order(venue, ['18:00 – 19:00'], amount=199 + 20)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'PACKAGE_REQUIRED')
        r = self.order(venue, ['18:00 – 19:00'], [{'name': 'Package — Squad', 'qty': 1}], amount=499 + 20)
        self.assertEqual(r.status_code, 201, r.data)

    def test_without_the_switch_the_hourly_path_stays_open(self):
        detail = {
            'timings': OPEN_ALL_DAY, 'unitPrices': ['199'],
            'packages': [{'label': 'Squad', 'price': '499', 'duration': '1'}],
        }
        venue = self.venue(detail, price=199)
        self.assertEqual(self.order(venue, ['18:00 – 19:00'], amount=219).status_code, 201)

    def test_the_string_false_does_not_require_a_package(self):
        detail = {
            'timings': OPEN_ALL_DAY, 'unitPrices': ['199'], 'packageRequired': 'false',
            'packages': [{'label': 'Squad', 'price': '499', 'duration': '1'}],
        }
        venue = self.venue(detail, price=199)
        self.assertEqual(self.order(venue, ['18:00 – 19:00'], amount=219).status_code, 201)

    def test_no_price_and_no_package_is_refused(self):
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['']}, price=0)
        r = self.order(venue, ['18:00 – 19:00'], amount=20)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.data['code'], 'PRICE_NOT_SET')

    def test_a_package_duration_written_with_units_is_enforced(self):
        """REGRESSION: float("3 hrs") failed, so such a package had no limit."""
        detail = {'timings': OPEN_ALL_DAY, 'unitPrices': ['2000'],
                  'packages': [{'label': 'Birthday', 'price': '4999', 'duration': '3 hrs'}]}
        venue = self.venue(detail, price=2000)
        long = self.order(venue, ['08:00 – 20:00'], [{'name': 'Package — Birthday', 'qty': 1}], amount=4999 + 20)
        self.assertEqual(long.status_code, 400)
        self.assertIn('please book exactly 3 hours', long.data['message'])
        ok = self.order(venue, ['18:00 – 21:00'], [{'name': 'Package — Birthday', 'qty': 1}], amount=4999 + 20)
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_a_package_is_one_continuous_block(self):
        detail = {'timings': OPEN_ALL_DAY, 'unitPrices': ['2000'],
                  'packages': [{'label': 'Birthday', 'price': '4999', 'duration': '3'}]}
        venue = self.venue(detail, price=2000)
        split = self.order(venue, ['08:00 – 09:30', '18:00 – 19:30'],
                           [{'name': 'Package — Birthday', 'qty': 1}], amount=4999 + 20)
        self.assertEqual(split.status_code, 400)
        self.assertIn('one continuous time', split.data['message'])
        joined = self.order(venue, ['18:00 – 19:30', '19:30 – 21:00'],
                            [{'name': 'Package — Birthday', 'qty': 1}], amount=4999 + 20)
        self.assertEqual(joined.status_code, 201, joined.data)

    def test_extra_hours_are_refused_without_a_package(self):
        detail = {'timings': OPEN_ALL_DAY, 'unitPrices': ['600'], 'extraHourPrice': '300', 'maxExtraHours': '3',
                  'packages': [{'label': 'Pack', 'price': '1500', 'duration': '2'}]}
        venue = self.venue(detail)
        r = self.order(venue, ['18:00 – 19:00'], [{'name': 'Extra hour', 'qty': 2}], amount=600 + 600 + 20)
        self.assertEqual(r.data['code'], 'EXTRA_HOURS_NEED_PACKAGE')
        r = self.order(venue, ['18:00 – 21:00'], [{'name': 'Package — Pack', 'qty': 1},
                                                  {'name': 'Extra hour', 'qty': 1}], amount=1500 + 300 + 20)
        self.assertEqual(r.status_code, 201, r.data)


@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret')
class QuoteTests(Fixtures, APITestCase):

    def test_quote_is_the_order_amount_and_writes_nothing(self):
        detail = {'timings': OPEN_ALL_DAY, 'unitPrices': ['600'],
                  'addons': [{'name': 'Cake', 'price': '500'}]}
        venue = self.venue(detail)
        q = self.quote(venue, ['18:00 – 20:00'], [{'name': 'Cake', 'qty': 2}])
        self.assertEqual(q.status_code, 200, q.data)
        quote = q.data['quote']
        self.assertEqual(quote['slotCharge'], 1200)
        self.assertEqual(quote['lines'], [{'name': 'Cake', 'qty': 2, 'price': 500, 'total': 1000}])
        self.assertEqual(quote['amount'], 1200 + 1000 + 20)
        self.assertEqual(quote['rate'], 600)
        self.assertFalse(Booking.objects.exists())
        r = self.order(venue, ['18:00 – 20:00'], [{'name': 'Cake', 'qty': 2}], amount=quote['amount'])
        self.assertEqual(r.status_code, 201, r.data)

    def test_a_tampered_client_price_does_not_change_the_quote(self):
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['600'],
                            'addons': [{'name': 'Cake', 'price': '500'}]})
        q = self.quote(venue, ['18:00 – 19:00'], [{'name': 'Cake', 'qty': 1, 'price': 1}], amount=5, perSlot=1)
        self.assertEqual(q.data['quote']['amount'], 600 + 500 + 20)

    def test_weekend_quote_uses_the_pitch_weekend_price_on_every_path(self):
        """REGRESSION (Stayfit): a court's own listing priced the weekend at
        its flat weekday price on the page, the server at the court's weekend
        price — the payment then failed with an amount mismatch."""
        sports = [{'name': 'Badminton', 'price': '500', 'units': 2,
                   'unitPrices': ['500', '500'], 'unitWeekendPrices': ['600', '600']}]
        base = self.venue({'timings': OPEN_ALL_DAY, 'sports': sports}, price=500)
        court2 = self.venue({'timings': OPEN_ALL_DAY, 'sports': sports, 'unitOf': str(base.id),
                             'unitLabel': 'Badminton · Court 2'}, price=500)
        saturday = next_weekday(5).isoformat()
        expected = 2 * 600 + 20
        for listing, unit in ((base, 1), (court2, 2)):
            q = self.quote(listing, ['18:00 – 20:00'], date=saturday, sport='Badminton', unit=unit)
            self.assertEqual(q.status_code, 200, q.data)
            self.assertEqual(q.data['quote']['amount'], expected)
            avail = self.client.get(f'/api/venues/{listing.id}/availability?date={saturday}').data
            hit = next(u for u in avail['unitRates'] if u['sport'] == 'Badminton' and u['unit'] == unit)
            self.assertEqual(hit['rate'], 600)        # what the page now prices with

    def test_quote_needs_a_signed_in_customer(self):
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['600']})
        self.client.force_authenticate(user=None)
        self.assertEqual(self.quote(venue, ['18:00 – 19:00']).status_code, 401)

    def test_quote_applies_and_reports_the_offer(self):
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['600'], 'offers': [
            {'title': 'Ten', 'code': 'TEN', 'type': 'percent', 'value': '10', 'minAmount': '', 'maxDiscount': '', 'expiry': ''},
        ]})
        q = self.quote(venue, ['18:00 – 20:00'], offer={'code': 'TEN', 'source': 'venue'})
        self.assertEqual(q.data['quote']['discount'], 120)
        self.assertEqual(q.data['quote']['amount'], 1200 - 120 + 20)
        bad = self.quote(venue, ['18:00 – 20:00'], offer={'code': 'NOPE', 'source': 'venue'})
        self.assertEqual(bad.status_code, 400)

    def test_availability_reports_the_effective_rate_and_package_rule(self):
        venue = self.venue(PACKAGE_ONLY, price=799)
        data = self.client.get(f'/api/venues/{venue.id}/availability?date={TOMORROW}').data
        self.assertTrue(data['packageRequired'])
        self.assertFalse(data['hourlyPriceSet'])
        self.assertTrue(all(u['rate'] > 0 for u in data['unitRates']))   # never ₹0


@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret')
class UnpublishedHoursTests(Fixtures, APITestCase):

    def test_a_venue_without_hours_is_not_bookable(self):
        venue = self.venue({'unitPrices': ['600']})
        avail = self.client.get(f'/api/venues/{venue.id}/availability?date={TOMORROW}').data
        self.assertTrue(avail['closed'])
        self.assertFalse(avail['hoursPublished'])
        self.assertEqual(avail['open'], [])
        hold = self.client.post('/api/bookings/holds', {
            'venueId': str(venue.id), 'date': TOMORROW, 'slots': ['18:00 – 19:00'], 'addons': [],
        }, format='json')
        self.assertEqual(hold.status_code, 400)
        self.assertEqual(hold.data['code'], 'HOURS_NOT_PUBLISHED')
        order = self.order(venue, ['18:00 – 19:00'], amount=620)
        self.assertEqual(order.data['code'], 'HOURS_NOT_PUBLISHED')
        self.assertFalse(Booking.objects.exists())

    def test_the_venue_page_says_whether_hours_are_published(self):
        without = self.venue({'unitPrices': ['600']})
        with_hours = self.venue({'unitPrices': ['600'], 'timings': {'open': '09:00', 'close': '22:00',
                                                                    'open24Hours': False}})
        self.assertFalse(self.client.get(f'/api/venues/{without.slug}').data['hoursPublished'])
        detail = self.client.get(f'/api/venues/{with_hours.slug}').data
        self.assertTrue(detail['hoursPublished'])
        self.assertFalse(detail['packageRequired'])

    def test_published_hours_bound_what_can_be_booked(self):
        venue = self.venue({'unitPrices': ['600'], 'timings': {'open': '09:00', 'close': '22:00',
                                                               'open24Hours': False}})
        avail = self.client.get(f'/api/venues/{venue.id}/availability?date={TOMORROW}').data
        self.assertTrue(avail['hoursPublished'])
        self.assertEqual(avail['open'], [{'start': '09:00', 'end': '22:00'}])
        self.assertEqual(self.order(venue, ['07:00 – 08:00'], amount=620).data['code'], 'OUTSIDE_HOURS')
        self.assertEqual(self.order(venue, ['10:00 – 11:00'], amount=620).status_code, 201)

    def test_the_date_strip_marks_unpublished_days(self):
        venue = self.venue({'unitPrices': ['600']})
        rows = self.client.get(f'/api/venues/{venue.id}/availability/days?days=3').data['days']
        self.assertTrue(all(r['status'] == 'closed' and r['hoursPublished'] is False for r in rows))


class DaySummaryQueryTests(Fixtures, APITestCase):

    def test_two_weeks_cost_the_same_queries_as_one_day(self):
        """REGRESSION (slow slots): two queries PER DAY made the 14-day strip
        take ~3.5 s on production."""
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['600']})
        url = f'/api/venues/{venue.id}/availability/days?days='
        self.client.get(url + '1')                       # warm caches
        with CaptureQueriesContext(connection) as one:
            self.assertEqual(self.client.get(url + '1').status_code, 200)
        with CaptureQueriesContext(connection) as fourteen:
            self.assertEqual(self.client.get(url + '14').status_code, 200)
        self.assertEqual(len(fourteen), len(one))

    def test_batched_summary_still_counts_bookings_per_day(self):
        venue = self.venue({'timings': OPEN_ALL_DAY, 'unitPrices': ['600']})
        day = today_ist() + datetime.timedelta(days=2)
        Booking.objects.create(listing=venue, user=self.customer, date=day, slots=['06:00 – 24:00'],
                               status='confirmed', amount=100, venue_name='x')
        rows = self.client.get(f'/api/venues/{venue.id}/availability/days?days=4').data['days']
        by_date = {r['date']: r for r in rows}
        self.assertEqual(by_date[day.isoformat()]['status'], 'sold_out')
        self.assertEqual(by_date[(day + datetime.timedelta(days=1)).isoformat()]['freeSlots'],
                         by_date[(day + datetime.timedelta(days=1)).isoformat()]['totalSlots'])
