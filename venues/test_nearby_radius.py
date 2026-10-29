"""
Nearby search acceptance tests — the customer's selected point, a radius,
and only the venues/events that are really inside it.

Reproduces the reported case: a customer in Medpally (Medchal–Malkajgiri)
must not be shown a Hanamkonda venue ~122 km away inside the default 30 km
nearby search. The coordinates below are real places, used as TEST DATA only
— the implementation knows nothing about either place.

The customer website always sends `radius` with a selected location
(src/constants/location.js NEARBY_DEFAULT_RADIUS_KM = 30); these tests pin the
server side of that contract.
"""
import datetime
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase

from accounts.models import User
from venues.geo import haversine_km
from venues.models import Listing
from venues.test_geo import fake_response, make_event, make_listing, place_link

MEDPALLY = (17.4075789, 78.6005299)       # Nominatim's pin for "Medpally"
HANAMKONDA = (18.0112761, 79.55879)       # a live venue's pin, ~122 km away
ORIGIN = {'lat': '17.408', 'lng': '78.601'}   # what the browser sends (3 dp)


def north_of(origin, km):
    """A point `km` due north of `origin` (1° latitude ≈ 111.19 km)."""
    return origin[0] + km / 111.19492664455854, origin[1]


class NearbyRadiusTests(APITestCase):
    def setUp(self):
        cache.clear()   # the list endpoint is response-cached for 60s
        vendor = User.objects.create_user(phone='9000000201', name='V', role=User.Role.VENDOR)
        self.near5 = make_listing(vendor, 'Five Km Hall', place_link(*north_of(MEDPALLY, 5)))
        self.near25 = make_listing(vendor, 'Twenty Five Km Turf', place_link(*north_of(MEDPALLY, 25)),
                                   category='Play zone')
        self.near35 = make_listing(vendor, 'Thirty Five Km Resort', place_link(*north_of(MEDPALLY, 35)),
                                   category='Resort')
        self.far = make_listing(vendor, 'Hanamkonda Theatre', place_link(*HANAMKONDA),
                                category='Private theatre')
        self.nopin = make_listing(vendor, 'Short Link Lounge', 'https://maps.app.goo.gl/abc',
                                  category='Playstation')
        # Newest first is: no-pin, far, 35, 25, 5 (explicit, unambiguous times).
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        for minutes, listing in enumerate((self.near5, self.near25, self.near35, self.far, self.nopin)):
            Listing.objects.filter(pk=listing.pk).update(
                created_at=start + datetime.timedelta(minutes=minutes)
            )

    def tearDown(self):
        cache.clear()

    def get(self, **params):
        response = self.client.get('/api/venues', {**ORIGIN, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response

    @staticmethod
    def names(response):
        return [v['name'] for v in response.data['venues']]

    def test_the_far_venue_really_is_about_120_km_away(self):
        self.assertAlmostEqual(haversine_km(*MEDPALLY, *HANAMKONDA), 121.7, delta=0.5)

    def test_a_venue_5_km_away_is_inside_30_km(self):
        r = self.get(radius='30')
        self.assertIn('Five Km Hall', self.names(r))
        row = next(v for v in r.data['venues'] if v['name'] == 'Five Km Hall')
        self.assertAlmostEqual(row['distanceKm'], 5.0, delta=0.1)

    def test_a_venue_120_km_away_is_excluded_from_30_km(self):
        r = self.get(radius='30')
        self.assertNotIn('Hanamkonda Theatre', self.names(r))
        self.assertEqual(r.data['total'], 2)                       # 5 km + 25 km only
        self.assertTrue(all(v['distanceKm'] <= 30 for v in r.data['venues']))

    def test_20_and_40_km_change_the_results(self):
        self.assertEqual(sorted(self.names(self.get(radius='20'))), ['Five Km Hall'])
        self.assertEqual(
            sorted(self.names(self.get(radius='40'))),
            ['Five Km Hall', 'Thirty Five Km Resort', 'Twenty Five Km Turf'],
        )
        # A wider radius never lets the ~122 km venue in until it is reached.
        self.assertNotIn('Hanamkonda Theatre', self.names(self.get(radius='120')))
        self.assertIn('Hanamkonda Theatre', self.names(self.get(radius='125')))

    def test_nearest_first_sorts_by_calculated_distance(self):
        r = self.get(radius='40', sort='distance')
        self.assertEqual(self.names(r), ['Five Km Hall', 'Twenty Five Km Turf', 'Thirty Five Km Resort'])
        distances = [v['distanceKm'] for v in r.data['venues']]
        self.assertEqual(distances, sorted(distances))

    def test_newest_first_is_kept_inside_the_radius(self):
        r = self.get(radius='40', sort='new')
        self.assertEqual(self.names(r), ['Thirty Five Km Resort', 'Twenty Five Km Turf', 'Five Km Hall'])

    def test_radius_with_category_filter_and_pagination(self):
        r = self.get(radius='40', category='Resort')
        self.assertEqual(self.names(r), ['Thirty Five Km Resort'])
        r = self.get(radius='40', sort='distance', limit='2', page='2')
        self.assertEqual(self.names(r), ['Thirty Five Km Resort'])
        self.assertEqual(r.data['total'], 3)

    def test_missing_pins_get_no_distance_and_are_not_nearby(self):
        rows = {v['name']: v for v in self.get().data['venues']}   # location, no radius
        self.assertIsNone(rows['Short Link Lounge']['distanceKm'])  # never invented
        self.assertIsNone(rows['Short Link Lounge']['coordinates'])
        self.assertNotIn('Short Link Lounge', self.names(self.get(radius='500')))

    def test_a_client_supplied_distance_is_ignored(self):
        # Only lat/lng/radius are read; the distance is always the server's.
        r = self.get(radius='30', distanceKm='1', distance='1')
        rows = {v['name']: v for v in r.data['venues']}
        self.assertAlmostEqual(rows['Twenty Five Km Turf']['distanceKm'], 25.0, delta=0.1)

    def test_a_new_location_gives_new_results(self):
        r = self.client.get('/api/venues', {'lat': '18.011', 'lng': '79.559', 'radius': '30'})
        self.assertEqual(self.names(r), ['Hanamkonda Theatre'])


class NearbyEventsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.near = make_event('Medpally Dandiya', maps_link=place_link(*north_of(MEDPALLY, 5)))
        self.far = make_event('Hanamkonda Garba', maps_link=place_link(*HANAMKONDA))
        self.nopin = make_event('Unpinned Night', maps_link='https://maps.app.goo.gl/q')

    def tearDown(self):
        cache.clear()

    def test_events_use_the_same_radius_rule(self):
        r = self.client.get('/api/events', {**ORIGIN, 'radius': '30'})
        self.assertEqual([e['name'] for e in r.data['events']], ['Medpally Dandiya'])
        r = self.client.get('/api/events', {**ORIGIN, 'radius': '30', 'sort': 'distance'})
        self.assertEqual([e['name'] for e in r.data['events']], ['Medpally Dandiya'])


class GeocoderFailureTests(APITestCase):
    """Manual location search through the backend's provider — failures are
    reported as failures, never as a location, and never leak the key."""

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-secret-key')
    def test_a_manual_place_resolves_to_the_providers_coordinates(self):
        payload = {'status': 'OK', 'results': [{
            'formatted_address': 'Medipally, Telangana, India',
            'geometry': {'location': {'lat': MEDPALLY[0], 'lng': MEDPALLY[1]}},
            'address_components': [
                {'long_name': 'Medipally', 'types': ['sublocality_level_1', 'sublocality']},
                {'long_name': 'Medchal–Malkajgiri', 'types': ['administrative_area_level_3']},
            ],
        }]}
        with patch('venues.geocoding.requests.get', return_value=fake_response(payload)) as network:
            r = self.client.get('/api/geo/search', {'q': 'Medpally'})
            self.assertEqual(network.call_args.kwargs['params']['key'], 'server-secret-key')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['results'], [{
            'label': 'Medipally, Medchal–Malkajgiri', 'locality': 'Medipally',
            'city': 'Medchal–Malkajgiri', 'lat': MEDPALLY[0], 'lng': MEDPALLY[1],
        }])
        self.assertNotIn('server-secret-key', str(r.content))

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='')
    def test_missing_key_is_not_configured(self):
        with patch('venues.geocoding.requests.get') as network:
            r = self.client.get('/api/geo/search', {'q': 'Medpally'})
            network.assert_not_called()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['code'], 'GEOCODER_NOT_CONFIGURED')

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-secret-key')
    def test_an_unknown_address_is_an_empty_list_not_a_location(self):
        with patch('venues.geocoding.requests.get', return_value=fake_response({'status': 'ZERO_RESULTS'})):
            r = self.client.get('/api/geo/search', {'q': 'Nowhereville Xyz'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['results'], [])

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-secret-key')
    def test_quota_and_network_failures_never_leak_the_key_or_query(self):
        quota = fake_response({'status': 'OVER_QUERY_LIMIT', 'error_message': 'quota for key …'})
        http_error = MagicMock()
        http_error.raise_for_status.side_effect = requests.HTTPError(
            '403 Client Error: Forbidden for url: https://maps.googleapis.com/maps/api/geocode/'
            'json?address=Medpally&key=server-secret-key',
            response=MagicMock(status_code=403),
        )
        network_error = requests.ConnectionError(
            "HTTPSConnectionPool(host='maps.googleapis.com'): Max retries exceeded with url: "
            '/maps/api/geocode/json?address=Medpally&key=server-secret-key'
        )
        for case in ({'return_value': quota}, {'return_value': http_error}, {'side_effect': network_error}):
            cache.clear()
            with self.assertLogs('venues.geocoding', 'WARNING') as logs, \
                    patch('venues.geocoding.requests.get', **case):
                r = self.client.get('/api/geo/search', {'q': 'Medpally'})
            self.assertEqual(r.status_code, 502, case)
            self.assertEqual(r.data['code'], 'GEOCODER_UNAVAILABLE')
            self.assertNotIn('results', r.data)        # no coordinates on failure
            logged = '\n'.join(logs.output)
            self.assertNotIn('server-secret-key', logged)
            self.assertNotIn('Medpally', logged)
            self.assertNotIn('server-secret-key', str(r.content))
