"""
Location-based discovery: map pins, distances, nearest-first sorting, radius
filtering and the place-search proxy.

The rule every test here protects: a distance is only ever computed from a
REAL pin (the vendor's own maps link) and the customer's selected point —
never guessed, never shown for a venue without a pin.
"""
import datetime
import importlib
import math
import uuid
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from events.models import Event, EventDate, TicketType
from venues.geo import (
    EARTH_RADIUS_KM,
    LocationParamError,
    bounding_box,
    distance_km,
    haversine_km,
    parse_location_params,
    pin_from_maps_link,
    rank_by_distance,
    record_pin,
)
from venues.models import Listing

ONE_DEGREE_KM = 2 * math.pi * EARTH_RADIUS_KM / 360   # 111.19 km along a meridian

# A real Google "place" URL shape: the @ viewport centre is NOT the venue;
# the trailing !3d!4d pair is the marker.
PLACE_LINK = (
    'https://www.google.com/maps/place/Grand+Hall/@17.4000,78.4000,15z/'
    'data=!3m1!4b1!4m6!3m5!1s0x0:0x1!8m2!3d17.4400!4d78.3489!16s'
)


def place_link(lat, lng):
    return f'https://www.google.com/maps/place/X/@0,0,15z/data=!4m5!3m4!8m2!3d{lat}!4d{lng}'


class PinParsingTests(SimpleTestCase):
    def test_place_marker_is_used_not_the_viewport(self):
        self.assertEqual(pin_from_maps_link(PLACE_LINK), (17.44, 78.3489))

    def test_last_marker_wins(self):
        link = 'https://www.google.com/maps/place/X/data=!3d1.5!4d2.5!3d17.1!4d78.2'
        self.assertEqual(pin_from_maps_link(link), (17.1, 78.2))

    def test_coordinate_query_params(self):
        for key in ('q', 'query', 'destination', 'daddr', 'll'):
            link = f'https://maps.google.com/?{key}=17.45,78.38'
            self.assertEqual(pin_from_maps_link(link), (17.45, 78.38), key)

    def test_links_without_a_pin_give_none(self):
        for link in (
            '', None, 'https://maps.app.goo.gl/AbCdEf',           # short link
            'https://www.google.com/maps/search/grand+hall',      # search
            'https://www.google.com/maps/@17.40,78.40,15z',       # viewport only
            'https://maps.google.com/?q=Grand+Hall+Hyderabad',    # text query
            'not a url',
        ):
            self.assertIsNone(pin_from_maps_link(link), link)

    def test_out_of_range_and_null_island_are_rejected(self):
        self.assertIsNone(pin_from_maps_link('https://maps.google.com/?q=95.0,78.0'))
        self.assertIsNone(pin_from_maps_link('https://maps.google.com/?q=17.0,190.0'))
        self.assertIsNone(pin_from_maps_link('https://maps.google.com/?q=0,0'))

    def test_record_pin_reads_detail_then_top_level(self):
        self.assertEqual(record_pin({'detail': {'mapsLink': PLACE_LINK}}), (17.44, 78.3489))
        self.assertEqual(record_pin({'mapsLink': PLACE_LINK}), (17.44, 78.3489))
        self.assertIsNone(record_pin({}))
        self.assertIsNone(record_pin(None))

    def test_resolved_short_link_is_used_only_for_the_current_link(self):
        short = 'https://maps.app.goo.gl/AbCdEf'
        record = {'detail': {'mapsLink': short, 'mapsLinkResolved': {'source': short, 'url': PLACE_LINK}}}
        self.assertEqual(record_pin(record), (17.44, 78.3489))
        # The vendor replaced the link: the old resolution must not linger.
        record['detail']['mapsLink'] = 'https://maps.app.goo.gl/Different'
        self.assertIsNone(record_pin(record))


class HaversineTests(SimpleTestCase):
    def test_one_degree_of_latitude(self):
        self.assertAlmostEqual(haversine_km(17.0, 78.0, 18.0, 78.0), ONE_DEGREE_KM, places=6)

    def test_one_degree_of_longitude_shrinks_with_latitude(self):
        expected = ONE_DEGREE_KM * math.cos(math.radians(60))
        self.assertAlmostEqual(haversine_km(60.0, 10.0, 60.0, 11.0), expected, delta=0.01)

    def test_symmetric_and_zero(self):
        self.assertEqual(haversine_km(17.4, 78.3, 17.4, 78.3), 0)
        self.assertAlmostEqual(
            haversine_km(17.4, 78.3, 17.9, 79.5), haversine_km(17.9, 79.5, 17.4, 78.3), places=9
        )

    def test_distance_km_rounds_and_handles_missing(self):
        self.assertEqual(distance_km((17.0, 78.0), 18.0, 78.0), round(ONE_DEGREE_KM, 2))
        self.assertIsNone(distance_km((17.0, 78.0), None, None))
        self.assertIsNone(distance_km(None, 17.0, 78.0))


class LocationParamTests(SimpleTestCase):
    def test_absent_is_fine(self):
        self.assertEqual(parse_location_params({}), (None, None))

    def test_valid(self):
        self.assertEqual(
            parse_location_params({'lat': '17.44', 'lng': '78.35', 'radius': '10'}),
            ((17.44, 78.35), 10.0),
        )

    def test_rejections(self):
        for params in (
            {'lat': '17.4'},                                   # half a pair
            {'lat': 'abc', 'lng': '78'},                       # junk
            {'lat': '91', 'lng': '78'},                        # out of range
            {'radius': '5'},                                   # radius around nowhere
            {'lat': '17', 'lng': '78', 'radius': '0'},
            {'lat': '17', 'lng': '78', 'radius': '-3'},
            {'lat': '17', 'lng': '78', 'radius': '9999'},
            {'lat': '17', 'lng': '78', 'radius': 'nan'},
        ):
            with self.assertRaises(LocationParamError, msg=params):
                parse_location_params(params)

    def test_bounding_box_contains_the_circle(self):
        min_lat, max_lat, min_lng, max_lng = bounding_box(17.44, 78.35, 10)
        for bearing in range(0, 360, 15):
            # A point exactly 10 km away in every direction is inside the box.
            d = 10 / EARTH_RADIUS_KM
            lat1, lng1, b = math.radians(17.44), math.radians(78.35), math.radians(bearing)
            lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
            lng2 = lng1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1),
                                     math.cos(d) - math.sin(lat1) * math.sin(lat2))
            self.assertTrue(min_lat <= math.degrees(lat2) <= max_lat)
            self.assertTrue(min_lng <= math.degrees(lng2) <= max_lng)

    def test_rank_sorting_versus_radius(self):
        rows = [('far', 18.0, 78.35), ('none', None, None), ('near', 17.45, 78.35)]
        keys, dist = rank_by_distance(rows, (17.44, 78.35), nearest_first=True)
        self.assertEqual(keys, ['near', 'far', 'none'])     # sorting keeps all
        keys, _ = rank_by_distance(rows, (17.44, 78.35), 5)
        self.assertEqual(keys, ['near'])                      # radius excludes
        self.assertIsNone(dist['none'])


def make_listing(vendor, name, maps_link='', *, status=Listing.Status.LIVE, category='Party hall',
                 detail_extra=None, slug=None):
    detail = {'mapsLink': maps_link, **(detail_extra or {})}
    return Listing.objects.create(
        id=uuid.uuid4(), vendor=vendor, slug=slug or name.lower().replace(' ', '-'),
        record={'name': name, 'category': category, 'location': 'Somewhere',
                'price': 1000, 'detail': detail},
        name=name, category=category, status=status,
    )


class ListingCoordinateSyncTests(TestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(phone='9000000101', name='V', role=User.Role.VENDOR)

    def test_pin_is_derived_on_save_and_cleared_when_the_link_loses_it(self):
        listing = make_listing(self.vendor, 'Hall', PLACE_LINK)
        self.assertEqual((listing.latitude, listing.longitude), (17.44, 78.3489))
        listing.record = {**listing.record, 'detail': {'mapsLink': 'https://maps.app.goo.gl/x'}}
        listing.save(update_fields=['record'])
        listing.refresh_from_db()
        self.assertIsNone(listing.latitude)
        self.assertIsNone(listing.longitude)

    def test_status_only_saves_keep_the_pin(self):
        listing = make_listing(self.vendor, 'Hall', PLACE_LINK)
        listing.status = Listing.Status.PAUSED
        listing.save(update_fields=['status'])
        listing.refresh_from_db()
        self.assertEqual((listing.latitude, listing.longitude), (17.44, 78.3489))

    def test_unit_sibling_without_a_pin_takes_its_base_venue_pin(self):
        base = make_listing(self.vendor, 'Arena', PLACE_LINK)
        unit = make_listing(self.vendor, 'Arena — Pitch 2', '', detail_extra={'unitOf': str(base.id)},
                            slug='arena-pitch-2')
        self.assertEqual((unit.latitude, unit.longitude), (17.44, 78.3489))

    def test_migration_backfill_parses_stored_links_only(self):
        with_pin = make_listing(self.vendor, 'A', PLACE_LINK)
        short = make_listing(self.vendor, 'B', 'https://maps.app.goo.gl/abc')
        Listing.objects.update(latitude=None, longitude=None)   # pre-migration state
        record_before = Listing.objects.get(pk=with_pin.pk).record
        migration = importlib.import_module('venues.migrations.0009_listing_coordinates')
        with patch('requests.get') as network:
            migration.backfill_coordinates(django_apps, None)
            network.assert_not_called()                          # never goes online
        with_pin.refresh_from_db()
        short.refresh_from_db()
        self.assertEqual((with_pin.latitude, with_pin.longitude), (17.44, 78.3489))
        self.assertIsNone(short.latitude)
        self.assertEqual(with_pin.record, record_before)         # record untouched


class VenueDistanceApiTests(APITestCase):
    ORIGIN = {'lat': '17.44', 'lng': '78.35'}

    def setUp(self):
        cache.clear()   # the list endpoint is response-cached for 60s
        self.vendor = User.objects.create_user(phone='9000000102', name='V', role=User.Role.VENDOR)
        # Created oldest → newest, so the default ("new") order is D, C, B, A.
        self.a = make_listing(self.vendor, 'Alpha Hall', place_link(17.441, 78.351))      # ~0.15 km
        self.b = make_listing(self.vendor, 'Beta Theatre', 'https://maps.google.com/?q=17.47,78.36',
                              category='Private theatre')                                  # ~3.5 km
        self.c = make_listing(self.vendor, 'Gamma Resort', 'https://maps.app.goo.gl/abc',
                              category='Resort')                                           # no pin
        self.d = make_listing(self.vendor, 'Delta Arena', place_link(17.9689, 79.5941),
                              category='Play zone')                                        # ~145 km
        make_listing(self.vendor, 'Hidden', place_link(17.44, 78.35), status=Listing.Status.PENDING)
        # Explicit creation times: back-to-back creates can share a timestamp
        # on a coarse clock, which would make "newest first" ambiguous.
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for minutes, listing in enumerate((self.a, self.b, self.c, self.d)):
            Listing.objects.filter(pk=listing.pk).update(
                created_at=start + datetime.timedelta(minutes=minutes)
            )

    def tearDown(self):
        cache.clear()

    def names(self, response):
        return [v['name'] for v in response.data['venues']]

    def test_without_location_nothing_changes_but_pins_are_exposed(self):
        r = self.client.get('/api/venues')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.names(r), ['Delta Arena', 'Gamma Resort', 'Beta Theatre', 'Alpha Hall'])
        rows = {v['name']: v for v in r.data['venues']}
        self.assertNotIn('distanceKm', rows['Alpha Hall'])
        self.assertEqual(rows['Alpha Hall']['coordinates'], {'lat': 17.441, 'lng': 78.351})
        self.assertIsNone(rows['Gamma Resort']['coordinates'])

    def test_location_adds_real_distances_and_null_for_no_pin(self):
        r = self.client.get('/api/venues', self.ORIGIN)
        rows = {v['name']: v for v in r.data['venues']}
        self.assertAlmostEqual(rows['Alpha Hall']['distanceKm'],
                               haversine_km(17.44, 78.35, 17.441, 78.351), delta=0.01)
        self.assertAlmostEqual(rows['Delta Arena']['distanceKm'],
                               haversine_km(17.44, 78.35, 17.9689, 79.5941), delta=0.01)
        self.assertIsNone(rows['Gamma Resort']['distanceKm'])
        # Same order as without a location: distance alone does not re-sort.
        self.assertEqual(self.names(r), ['Delta Arena', 'Gamma Resort', 'Beta Theatre', 'Alpha Hall'])

    def test_nearest_first_sorting_keeps_unpinned_venues_last(self):
        r = self.client.get('/api/venues', {**self.ORIGIN, 'sort': 'distance'})
        self.assertEqual(self.names(r), ['Alpha Hall', 'Beta Theatre', 'Delta Arena', 'Gamma Resort'])
        self.assertEqual(r.data['total'], 4)

    def test_radius_excludes_far_and_unpinned_venues(self):
        r = self.client.get('/api/venues', {**self.ORIGIN, 'radius': '5', 'sort': 'distance'})
        self.assertEqual(self.names(r), ['Alpha Hall', 'Beta Theatre'])
        self.assertEqual(r.data['total'], 2)
        r = self.client.get('/api/venues', {**self.ORIGIN, 'radius': '1'})
        self.assertEqual(self.names(r), ['Alpha Hall'])

    def test_radius_with_category_and_paging(self):
        r = self.client.get('/api/venues', {**self.ORIGIN, 'radius': '25', 'category': 'Private theatre'})
        self.assertEqual(self.names(r), ['Beta Theatre'])
        r = self.client.get('/api/venues', {**self.ORIGIN, 'sort': 'distance', 'limit': 2, 'page': 2})
        self.assertEqual(self.names(r), ['Delta Arena', 'Gamma Resort'])
        self.assertEqual(r.data['total'], 4)

    def test_a_different_location_gives_a_different_order(self):
        warangal = {'lat': '17.9689', 'lng': '79.5941', 'sort': 'distance'}
        r = self.client.get('/api/venues', warangal)
        self.assertEqual(self.names(r)[0], 'Delta Arena')

    def test_bad_location_params_are_400(self):
        for params in ({'lat': '17'}, {'sort': 'distance'}, {'radius': '5'},
                       {**self.ORIGIN, 'radius': 'x'}, {'lat': 'x', 'lng': 'y'}):
            r = self.client.get('/api/venues', params)
            self.assertEqual(r.status_code, 400, params)

    def test_detail_exposes_the_pin(self):
        r = self.client.get(f'/api/venues/{self.a.slug}')
        self.assertEqual(r.data['coordinates'], {'lat': 17.441, 'lng': 78.351})
        r = self.client.get(f'/api/venues/{self.c.slug}')
        self.assertIsNone(r.data['coordinates'])


def make_event(name, *, maps_link='', listing=None, status=Event.Status.LIVE):
    from bookings.slots import today_ist
    event = Event.objects.create(
        slug=name.lower().replace(' ', '-'), name=name, category='Dandiya',
        venue_name='Grounds', city='Warangal', maps_link=maps_link, listing=listing,
        start_time=datetime.time(18, 0), end_time=datetime.time(22, 0), status=status,
    )
    EventDate.objects.create(event=event, date=today_ist() + datetime.timedelta(days=1), capacity=100)
    TicketType.objects.create(event=event, key='adult', label='Adult', price=300)
    TicketType.objects.create(event=event, key='child', label='Child', price=150)
    return event


class EventDistanceApiTests(APITestCase):
    def setUp(self):
        cache.clear()
        vendor = User.objects.create_user(phone='9000000103', name='V', role=User.Role.VENDOR)
        venue = make_listing(vendor, 'Host Venue', place_link(17.46, 78.36))
        self.own = make_event('Own Pin Night', maps_link=place_link(17.441, 78.351))
        self.hosted = make_event('Hosted Night', listing=venue)                 # inherits venue pin
        self.nopin = make_event('Mystery Night', maps_link='https://maps.app.goo.gl/q')

    def tearDown(self):
        cache.clear()

    def test_event_pins_come_from_own_link_then_the_listed_venue(self):
        self.assertEqual((self.own.latitude, self.own.longitude), (17.441, 78.351))
        self.assertEqual((self.hosted.latitude, self.hosted.longitude), (17.46, 78.36))
        self.assertIsNone(self.nopin.latitude)

    def test_nearest_first_and_radius(self):
        origin = {'lat': '17.44', 'lng': '78.35'}
        r = self.client.get('/api/events', {**origin, 'sort': 'distance'})
        self.assertEqual([e['name'] for e in r.data['events']],
                         ['Own Pin Night', 'Hosted Night', 'Mystery Night'])
        self.assertIsNone(r.data['events'][2]['distanceKm'])
        r = self.client.get('/api/events', {**origin, 'radius': '1'})
        self.assertEqual([e['name'] for e in r.data['events']], ['Own Pin Night'])

    def test_event_detail_has_coordinates(self):
        r = self.client.get(f'/api/events/{self.own.slug}')
        self.assertEqual(r.data['coordinates'], {'lat': 17.441, 'lng': 78.351})


def fake_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class GeocodingProxyTests(APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_off_by_default_never_invents_coordinates(self):
        with patch('venues.geocoding.requests.get') as network:
            r = self.client.get('/api/geo/search', {'q': 'Gachibowli'})
            network.assert_not_called()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['code'], 'GEOCODER_NOT_CONFIGURED')
        r = self.client.get('/api/geo/reverse', {'lat': '17.44', 'lng': '78.35'})
        self.assertEqual(r.status_code, 503)

    @override_settings(GEOCODER_PROVIDER='nominatim', GEOCODER_CONTACT_EMAIL='')
    def test_nominatim_requires_a_contact_email(self):
        r = self.client.get('/api/geo/search', {'q': 'Gachibowli'})
        self.assertEqual(r.data['code'], 'GEOCODER_NOT_CONFIGURED')

    @override_settings(GEOCODER_PROVIDER='nominatim', GEOCODER_CONTACT_EMAIL='ops@example.com')
    def test_nominatim_search_and_cache(self):
        payload = [{
            'lat': '17.4401', 'lon': '78.3489', 'display_name': 'Gachibowli, Hyderabad, Telangana',
            'address': {'suburb': 'Gachibowli', 'city': 'Hyderabad'},
        }, {'lat': 'junk', 'lon': '1', 'address': {'suburb': 'Bad'}}]
        with patch('venues.geocoding.requests.get', return_value=fake_response(payload)) as network:
            r = self.client.get('/api/geo/search', {'q': '  Gachibowli '})
            self.client.get('/api/geo/search', {'q': 'gachibowli'})       # cached
            self.assertEqual(network.call_count, 1)
            params = network.call_args.kwargs['params']
            self.assertEqual(params['countrycodes'], 'in')
            self.assertIn('ops@example.com', network.call_args.kwargs['headers']['User-Agent'])
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['results'], [{
            'label': 'Gachibowli, Hyderabad', 'locality': 'Gachibowli', 'city': 'Hyderabad',
            'lat': 17.4401, 'lng': 78.3489,
            # Hyderabad is also a district name found in one state only.
            'district': 'Hyderabad', 'state': 'Telangana', 'country': 'India',
            'formattedAddress': 'Gachibowli, Hyderabad, Telangana',
        }])

    @override_settings(GEOCODER_PROVIDER='nominatim', GEOCODER_CONTACT_EMAIL='ops@example.com')
    def test_nominatim_reverse(self):
        payload = {'address': {'neighbourhood': 'Kondapur', 'city': 'Hyderabad'}}
        with patch('venues.geocoding.requests.get', return_value=fake_response(payload)) as network:
            r = self.client.get('/api/geo/reverse', {'lat': '17.4612345', 'lng': '78.3598765'})
            # The provider only ever sees the ~110 m-rounded point.
            self.assertEqual(network.call_args.kwargs['params']['lat'], 17.461)
        self.assertEqual(r.data['result'], {'label': 'Kondapur, Hyderabad', 'locality': 'Kondapur',
                                            'city': 'Hyderabad', 'district': 'Hyderabad',
                                            'state': 'Telangana', 'country': 'India',
                                            'formattedAddress': ''})

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-key')
    def test_google_search_and_error_status(self):
        payload = {'status': 'OK', 'results': [{
            'formatted_address': 'Hanamkonda, Telangana, India',
            'geometry': {'location': {'lat': 18.0072, 'lng': 79.5584}},
            'address_components': [
                {'long_name': 'Hanamkonda', 'types': ['locality', 'political']},
            ],
        }]}
        with patch('venues.geocoding.requests.get', return_value=fake_response(payload)) as network:
            r = self.client.get('/api/geo/search', {'q': 'Hanamkonda'})
            self.assertEqual(network.call_args.kwargs['params']['components'], 'country:IN')
        self.assertEqual(r.data['results'][0]['lat'], 18.0072)
        self.assertEqual(r.data['results'][0]['label'], 'Hanamkonda')
        cache.clear()
        denied = {'status': 'REQUEST_DENIED', 'error_message': 'API key details…'}
        with patch('venues.geocoding.requests.get', return_value=fake_response(denied)):
            r = self.client.get('/api/geo/search', {'q': 'Hanamkonda'})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn('API key', str(r.data))     # never leak provider config

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-key')
    def test_input_validation(self):
        self.assertEqual(self.client.get('/api/geo/search', {'q': 'a'}).status_code, 400)
        self.assertEqual(self.client.get('/api/geo/search', {'q': 'x' * 101}).status_code, 400)
        self.assertEqual(self.client.get('/api/geo/reverse', {'lat': 'x', 'lng': '1'}).status_code, 400)
