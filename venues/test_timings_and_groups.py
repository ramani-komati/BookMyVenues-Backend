"""
Venue Timings (venues/timings.py) and the primary categories
(venues/taxonomy.py CATEGORY_GROUPS) — regression tests.

Timings:
  * validation: open/close required unless open 24 hours, same time refused,
    garbage refused, after-midnight closing allowed
  * the booking grid: rounding inwards to 30 minutes, overnight venues,
    24 hours = the whole grid
  * publish validates them server-side and keeps the stored ones when a
    republish omits them
  * the booking engine uses them as the venue's default hours; detailed
    weekly hours (Business controls) still win
  * the public venue page shows them

Categories:
  * legacy values map onto the new primary categories (Playzone → Sports,
    PlayStation → Gaming, single-sport play zones → Sports, gyms → Fitness,
    open-air spellings → Celebrate)
  * /api/config serves the groups; venue rows carry categoryGroup
"""
import datetime
import uuid

from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from accounts.models import User
from bookings.slots import today_ist

from .business import check_bookable, open_periods
from .models import Listing, VenueBusinessSettings, VenueDraft
from .taxonomy import canonical_category, category_group
from .timings import (
    TimingsError, bookable_periods, from_draft, timings_label, validate_timings,
)

H = 60


class TimingsRulesTests(SimpleTestCase):
    def test_valid_and_normalised(self):
        self.assertEqual(validate_timings({'open': '9:00', 'close': '22:00'}),
                         {'open': '09:00', 'close': '22:00', 'open24Hours': False})
        self.assertEqual(validate_timings({'open24Hours': True, 'open': '10:00'}),
                         {'open': '', 'close': '', 'open24Hours': True})
        self.assertIsNone(validate_timings(None))
        self.assertIsNone(validate_timings({'open': '', 'close': ''}))

    def test_refusals(self):
        for bad in ({'open': '09:00'}, {'close': '22:00'}, {'open': '25:00', 'close': '22:00'},
                    {'open': 'nine', 'close': '22:00'}, {'open': '10:00', 'close': '10:00'},
                    {'open': '01:00', 'close': '05:00'}, 'x'):
            with self.assertRaises(TimingsError, msg=bad):
                validate_timings(bad)

    def test_after_midnight_is_allowed(self):
        self.assertEqual(validate_timings({'open': '18:00', 'close': '02:00'})['close'], '02:00')
        self.assertEqual(timings_label({'open': '18:00', 'close': '02:00'}), '6:00 PM – 2:00 AM (next day)')

    def test_labels(self):
        self.assertEqual(timings_label({'open24Hours': True}), 'Open 24 hours')
        self.assertEqual(timings_label({'open': '09:00', 'close': '22:00'}), '9:00 AM – 10:00 PM')
        self.assertEqual(timings_label(None), '')

    def test_booking_grid(self):
        self.assertEqual(bookable_periods(None), [(6 * H, 24 * H)])
        self.assertEqual(bookable_periods({'open24Hours': True}), [(6 * H, 24 * H)])
        self.assertEqual(bookable_periods({'open': '09:00', 'close': '22:00'}), [(9 * H, 22 * H)])
        # Rounded INWARDS: never bookable before opening or after closing.
        self.assertEqual(bookable_periods({'open': '09:10', 'close': '21:50'}), [(9 * H + 30, 21 * H + 30)])
        self.assertEqual(bookable_periods({'open': '05:00', 'close': '00:00'}), [(6 * H, 24 * H)])
        # Overnight: evening to midnight, plus the early morning run-on.
        self.assertEqual(bookable_periods({'open': '18:00', 'close': '02:00'}), [(18 * H, 24 * H)])
        self.assertEqual(bookable_periods({'open': '22:00', 'close': '07:00'}),
                         [(6 * H, 7 * H), (22 * H, 24 * H)])

    def test_from_draft(self):
        self.assertIsNone(from_draft({'venueName': 'x'}))
        self.assertEqual(from_draft({'openTime': '09:00', 'closeTime': '21:00', 'open24Hours': False}),
                         {'open': '09:00', 'close': '21:00', 'open24Hours': False})


class CategoryGroupTests(SimpleTestCase):
    def test_legacy_values_map_to_the_new_categories(self):
        cases = {
            'Playzone': 'sports', 'Play zone': 'sports', 'Box cricket': 'sports',
            'Swimming pool': 'sports', 'Pickleball': 'sports',
            'Playstation': 'gaming', 'PlayStation': 'gaming', 'Gaming lounge': 'gaming',
            'Private Hall': 'celebrate', 'Party hall': 'celebrate', 'Private Theatre': 'celebrate',
            'Open Theatre': 'celebrate', 'Open-air theatre': 'celebrate',
            'Resort': 'resorts', 'Gym': 'fitness', 'Fitness': 'fitness',
            'Iron Gym Studio': 'fitness',          # a vendor's own "Others" name
            'Rooftop Lounge': '',                  # genuinely uncategorised
        }
        for raw, group in cases.items():
            self.assertEqual(category_group(raw), group, raw)

    def test_open_theatre_is_its_own_type(self):
        self.assertEqual(canonical_category('Open Theatre'), 'Open-air theatre')
        self.assertEqual(canonical_category('Gym'), 'Gym')


class ListingFixtureMixin:
    def make_vendor(self, phone='9300000001'):
        return User.objects.create_user(phone=phone, name='Vendor', email=f'{phone}@example.com',
                                        role=User.Role.VENDOR)

    def make_listing(self, vendor, timings=None, category='Party hall', **record):
        detail = {'timings': timings} if timings is not None else {}
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=vendor, slug=f's-{uuid.uuid4().hex[:6]}',
            record={'name': 'Hall', 'price': 600, 'category': category, 'detail': detail, **record},
            name='Hall', category=category, locality='x', pincode='560001', status='live',
        )


class TimingsEngineTests(ListingFixtureMixin, APITestCase):
    def setUp(self):
        cache.clear()
        self.vendor = self.make_vendor()
        self.day = today_ist() + datetime.timedelta(days=2)

    def test_timings_are_the_default_hours(self):
        listing = self.make_listing(self.vendor, {'open': '10:00', 'close': '20:00', 'open24Hours': False})
        self.assertEqual(open_periods(None, self.day, listing=listing)[0], [(10 * H, 20 * H)])
        from bookings.slots import SlotError
        with self.assertRaises(SlotError) as caught:
            check_bookable(listing, self.day, [(8 * H, 9 * H)])
        self.assertEqual(caught.exception.code, 'OUTSIDE_HOURS')
        check_bookable(listing, self.day, [(11 * H, 12 * H)])   # inside: fine

    def test_no_timings_keeps_the_platform_default(self):
        listing = self.make_listing(self.vendor)
        self.assertEqual(open_periods(None, self.day, listing=listing)[0], [(6 * H, 24 * H)])

    def test_weekly_hours_from_business_controls_win(self):
        listing = self.make_listing(self.vendor, {'open': '10:00', 'close': '20:00', 'open24Hours': False})
        hours = {str(i): [{'start': '07:00', 'end': '09:00'}] for i in range(7)}
        settings = VenueBusinessSettings.objects.create(listing=listing, hours=hours)
        self.assertEqual(open_periods(settings, self.day)[0], [(7 * H, 9 * H)])
        # The settings row with no weekly hours falls back to the timings.
        settings.hours = None
        settings.save()
        self.assertEqual(open_periods(settings, self.day)[0], [(10 * H, 20 * H)])

    def test_public_detail_shows_the_saved_hours(self):
        listing = self.make_listing(self.vendor, {'open': '18:00', 'close': '02:00', 'open24Hours': False})
        detail = self.client.get(f'/api/venues/{listing.pk}').data['detail']
        self.assertEqual(detail['timingsLabel'], '6:00 PM – 2:00 AM (next day)')
        self.assertEqual(detail['timings']['open'], '18:00')

    def test_availability_marks_hours_outside_the_timings_closed(self):
        listing = self.make_listing(self.vendor, {'open': '10:00', 'close': '20:00', 'open24Hours': False})
        data = self.client.get(f'/api/venues/{listing.pk}/availability?date={self.day.isoformat()}').data
        self.assertEqual(data['open'], [{'start': '10:00', 'end': '20:00'}])


class TimingsPublishTests(ListingFixtureMixin, APITestCase):
    def setUp(self):
        cache.clear()
        self.vendor = self.make_vendor('9300000002')
        self.client.force_authenticate(user=self.vendor)
        self.draft = VenueDraft.objects.create(vendor=self.vendor)

    def publish(self, timings=..., **extra):
        detail = {'description': 'x'}
        if timings is not ...:
            detail['timings'] = timings
        return self.client.post('/api/vendors/me/listings', {
            'id': str(self.draft.pk), 'name': 'Hall', 'category': 'Party hall',
            'price': '600', 'detail': detail, **extra,
        }, format='json')

    def test_valid_timings_are_saved_normalised(self):
        response = self.publish({'open': '9:00', 'close': '23:00', 'open24Hours': False})
        self.assertEqual(response.status_code, 201)
        stored = Listing.objects.get(pk=self.draft.pk).record['detail']['timings']
        self.assertEqual(stored, {'open': '09:00', 'close': '23:00', 'open24Hours': False})

    def test_invalid_timings_are_refused(self):
        response = self.publish({'open': '10:00', 'close': '10:00', 'open24Hours': False})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Listing.objects.filter(pk=self.draft.pk).exists())

    def test_republish_without_timings_keeps_the_stored_ones(self):
        self.publish({'open24Hours': True})
        self.assertEqual(self.publish().status_code, 201)          # older client
        stored = Listing.objects.get(pk=self.draft.pk).record['detail']['timings']
        self.assertTrue(stored['open24Hours'])


class GroupApiTests(ListingFixtureMixin, APITestCase):
    def test_config_serves_the_groups(self):
        groups = self.client.get('/api/config').data['categoryGroups']
        self.assertEqual([g['key'] for g in groups], ['celebrate', 'sports', 'gaming', 'resorts', 'fitness'])
        celebrate = groups[0]
        # The original three keep their place; Big Screen Surprise (added
        # 2026-10-30) joins them as Celebrate's fourth venue type.
        self.assertEqual(
            celebrate['types'],
            ['Party hall', 'Private theatre', 'Open-air theatre', 'Big screen surprise'],
        )
        self.assertNotIn('events', [g['key'] for g in groups])
        self.assertNotIn('explore', [g['key'] for g in groups])

    def test_venue_rows_carry_their_group(self):
        cache.clear()
        vendor = self.make_vendor('9300000003')
        self.make_listing(vendor, category='Playzone')
        self.make_listing(vendor, category='Gym')
        rows = self.client.get('/api/venues?limit=50').data['venues']
        self.assertEqual(sorted(r['categoryGroup'] for r in rows), ['fitness', 'sports'])
        self.assertIn('Play zone', [r['category'] for r in rows])
