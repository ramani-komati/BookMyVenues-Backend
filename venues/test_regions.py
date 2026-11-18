"""
Pan-India locations: the states/districts dataset, PIN rules, the vendor
submit gate, the vendor-confirmed map pin, and the Places-based
autocomplete / place-details / regions endpoints.

The rule these protect: a venue can be registered in ANY Indian state or
union territory, and every place name is matched to ONE canonical spelling —
never invented.
"""
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from . import regions
from .completion import compute_completion
from .draft_validation import validate_section
from .geo import record_pin
from .models import VenueDraft


class DatasetTests(SimpleTestCase):
    def test_every_state_and_union_territory_is_present(self):
        kinds = [s['type'] for s in regions.states()]
        self.assertEqual(kinds.count('state'), 28)
        self.assertEqual(kinds.count('ut'), 8)
        names = {s['name'] for s in regions.states()}
        for name in ('Telangana', 'Karnataka', 'Maharashtra', 'Tamil Nadu', 'Kerala', 'Delhi',
                     'Rajasthan', 'Andhra Pradesh', 'Ladakh', 'Lakshadweep', 'Puducherry',
                     'Jammu and Kashmir', 'Dadra and Nagar Haveli and Daman and Diu'):
            self.assertIn(name, names)

    def test_districts_are_complete_lists_not_a_few_cities(self):
        total = sum(len(s['districts']) for s in regions.states())
        self.assertGreater(total, 750)
        self.assertEqual(len(regions.find_state('Telangana')['districts']), 33)
        for state in regions.states():
            self.assertTrue(state['districts'], state['name'])
            self.assertEqual(len(state['districts']), len(set(state['districts'])), state['name'])

    def test_state_names_codes_and_aliases_resolve(self):
        for given, expected in (
            ('telangana', 'Telangana'), ('TS', 'Telangana'), ('TG', 'Telangana'),
            ('Orissa', 'Odisha'), ('Pondicherry', 'Puducherry'), ('NCT of Delhi', 'Delhi'),
            ('Jammu & Kashmir', 'Jammu and Kashmir'), ('KA', 'Karnataka'),
        ):
            self.assertEqual(regions.canonical_state(given), expected, given)
        self.assertIsNone(regions.canonical_state('Atlantis'))
        self.assertIsNone(regions.canonical_state(''))

    def test_district_spellings_resolve_inside_their_state_only(self):
        self.assertEqual(regions.find_district('Telangana', 'Medchal–Malkajgiri'), 'Medchal Malkajgiri')
        self.assertEqual(regions.find_district('Telangana', 'Rangareddy District'), 'Ranga Reddy')
        self.assertEqual(regions.find_district('Telangana', 'Hanamkonda'), 'Hanumakonda')
        self.assertEqual(regions.find_district('Karnataka', 'Bangalore Urban'), 'Bengaluru Urban')
        self.assertEqual(regions.find_district('Maharashtra', 'Aurangabad'), 'Chhatrapati Sambhajinagar')
        self.assertEqual(regions.find_district('Delhi', 'South West Delhi'), 'South West')
        self.assertIsNone(regions.find_district('Karnataka', 'Warangal'))   # wrong state
        self.assertIsNone(regions.find_district('Atlantis', 'Warangal'))

    def test_district_anywhere_only_when_unambiguous(self):
        self.assertEqual(regions.find_district_anywhere('Warangal'), ('Telangana', 'Warangal'))
        self.assertIsNone(regions.find_district_anywhere('Bilaspur'))       # CG and HP
        self.assertIsNone(regions.find_district_anywhere(''))


class PincodeTests(SimpleTestCase):
    def test_format(self):
        self.assertIsNone(regions.pincode_error('500084'))
        for bad in ('', '50008', '5000845', '050084', 'abcdef', None):
            self.assertIsNotNone(regions.pincode_error(bad), bad)

    def test_army_postal_codes_are_not_venues(self):
        self.assertIsNotNone(regions.pincode_error('999001'))

    def test_the_postal_zone_must_match_the_state(self):
        self.assertIsNone(regions.pincode_error('500084', 'Telangana'))
        self.assertIsNone(regions.pincode_error('560001', 'Karnataka'))
        self.assertIsNone(regions.pincode_error('400001', 'Maharashtra'))
        self.assertIsNone(regions.pincode_error('110001', 'Delhi'))
        self.assertIsNone(regions.pincode_error('682001', 'Kerala'))
        self.assertIsNone(regions.pincode_error('533464', 'Puducherry'))   # Yanam
        self.assertIsNone(regions.pincode_error('605001', 'Puducherry'))
        self.assertIn('not in Karnataka', regions.pincode_error('400001', 'Karnataka'))
        self.assertIsNone(regions.pincode_error('400001', 'Atlantis'))      # unknown state: format only


def _draft(location):
    base = {
        'houseStreet': '12 Main Rd', 'pincode': '500084', 'stateName': 'Telangana',
        'district': 'Ranga Reddy', 'city': 'Hyderabad', 'mapsLink': 'https://maps.google.com/?q=x',
    }
    return VenueDraft(data={'location': {**base, **location}})


def _location_gaps(location):
    missing = compute_completion(_draft(location))[1]
    location_words = ('Street', 'Pincode', 'pincode', 'State', 'District', 'City', 'Maps')
    return [m for m in missing if any(w in m for w in location_words)]


class SubmitGateTests(SimpleTestCase):
    def test_a_complete_telangana_address_passes(self):
        self.assertEqual(_location_gaps({}), [])

    def test_venues_register_in_any_state(self):
        for state, district, city, pincode in (
            ('Karnataka', 'Bengaluru Urban', 'Bengaluru', '560034'),
            ('Maharashtra', 'Pune', 'Pune', '411001'),
            ('Tamil Nadu', 'Chennai', 'Chennai', '600001'),
            ('Rajasthan', 'Jaipur', 'Jaipur', '302001'),
            ('Ladakh', 'Leh', 'Leh', '194101'),
            ('Assam', 'Kamrup Metro', 'Guwahati', '781001'),
        ):
            gaps = _location_gaps({'stateName': state, 'district': district, 'city': city, 'pincode': pincode})
            self.assertEqual(gaps, [], state)

    def test_district_and_city_are_required(self):
        gaps = _location_gaps({'district': '', 'city': ''})
        self.assertIn('District', gaps)
        self.assertIn('City / Town', gaps)

    def test_unknown_state_wrong_district_and_wrong_pin_are_reported(self):
        self.assertIn('State (choose an Indian state or union territory)',
                      _location_gaps({'stateName': 'Atlantis'}))
        self.assertIn('District (choose a district in Karnataka)',
                      _location_gaps({'stateName': 'Karnataka', 'pincode': '560001', 'district': 'Warangal'}))
        self.assertIn('A valid pincode for the venue’s state',
                      _location_gaps({'stateName': 'Karnataka', 'district': 'Mysuru', 'pincode': '400001'}))

    def test_older_spellings_from_saved_drafts_still_pass(self):
        self.assertEqual(_location_gaps({'stateName': 'TS', 'district': 'Medchal–Malkajgiri'}), [])


class DraftFormatTests(SimpleTestCase):
    def test_pincode_cannot_start_with_zero(self):
        self.assertIsNotNone(validate_section('location', {'pincode': '012345'}))
        self.assertIsNone(validate_section('location', {'pincode': '500084'}))

    def test_map_pin_needs_both_halves_inside_india(self):
        self.assertIsNone(validate_section('location', {'latitude': '', 'longitude': ''}))
        self.assertIsNone(validate_section('location', {'latitude': 17.385, 'longitude': 78.4867}))
        self.assertIsNotNone(validate_section('location', {'latitude': 17.385}))
        self.assertIsNotNone(validate_section('location', {'latitude': 'x', 'longitude': '78'}))
        self.assertEqual(validate_section('location', {'latitude': 51.5, 'longitude': -0.12}),
                         'Map location must be inside India.')


class MapPinTests(SimpleTestCase):
    LINK_PIN = 'https://www.google.com/maps/place/X/data=!3d17.44!4d78.35'

    def test_the_vendors_confirmed_pin_fills_in_for_a_link_without_one(self):
        record = {'detail': {'mapsLink': 'https://maps.app.goo.gl/AbC', 'mapPin': {'lat': 18.0, 'lng': 79.55}}}
        self.assertEqual(record_pin(record), (18.0, 79.55))

    def test_an_exact_link_pin_always_wins(self):
        record = {'detail': {'mapsLink': self.LINK_PIN, 'mapPin': {'lat': 18.0, 'lng': 79.55}}}
        self.assertEqual(record_pin(record), (17.44, 78.35))

    def test_a_broken_pin_is_ignored(self):
        self.assertIsNone(record_pin({'detail': {'mapPin': {'lat': 'x', 'lng': 1}}}))
        self.assertIsNone(record_pin({'detail': {'mapPin': {'lat': 0, 'lng': 0}}}))


def fake_http(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class GoogleNamesTests(APITestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-key')
    def test_search_reports_state_and_canonical_district(self):
        payload = {'status': 'OK', 'results': [{
            'formatted_address': 'Kompally, Hyderabad, Telangana 500100, India',
            'geometry': {'location': {'lat': 17.5354, 'lng': 78.4836}},
            'address_components': [
                {'long_name': 'Kompally', 'short_name': 'Kompally', 'types': ['sublocality_level_1', 'sublocality']},
                {'long_name': 'Hyderabad', 'short_name': 'Hyderabad', 'types': ['locality']},
                {'long_name': 'Medchal–Malkajgiri', 'short_name': 'Medchal–Malkajgiri',
                 'types': ['administrative_area_level_3']},
                {'long_name': 'Telangana', 'short_name': 'TG', 'types': ['administrative_area_level_1']},
                {'long_name': 'India', 'short_name': 'IN', 'types': ['country']},
            ],
        }]}
        with patch('venues.geocoding.requests.get', return_value=fake_http(payload)):
            r = self.client.get('/api/geo/search', {'q': 'Kompally'})
        row = r.data['results'][0]
        self.assertEqual((row['locality'], row['city'], row['district'], row['state'], row['country']),
                         ('Kompally', 'Hyderabad', 'Medchal Malkajgiri', 'Telangana', 'India'))
        self.assertEqual((row['lat'], row['lng']), (17.5354, 78.4836))

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-key')
    def test_search_can_be_scoped_to_a_district(self):
        with patch('venues.geocoding.requests.get', return_value=fake_http({'status': 'ZERO_RESULTS'})) as net:
            r = self.client.get('/api/geo/search', {'q': 'Kompally', 'state': 'TS', 'district': 'Medchal'})
        self.assertEqual(net.call_args.kwargs['params']['address'], 'Kompally, Medchal Malkajgiri, Telangana')
        self.assertEqual(r.data['results'], [])    # invalid place → empty, not an error

    @override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='server-key')
    def test_googles_refusal_reason_is_logged_but_never_returned(self):
        denied = {'status': 'REQUEST_DENIED',
                  'error_message': 'API keys with referer restrictions cannot be used with this API.'}
        with patch('venues.geocoding.requests.get', return_value=fake_http(denied)), \
                self.assertLogs('venues.geocoding', level='WARNING') as logs:
            r = self.client.get('/api/geo/search', {'q': 'Hyderabad'})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn('referer', str(r.data))
        self.assertIn('referer restrictions', '\n'.join(logs.output))
        self.assertNotIn('server-key', '\n'.join(logs.output))


@override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='geo-key',
                   GOOGLE_PLACES_API_KEY='places-key')
class AutocompleteTests(APITestCase):
    SUGGESTIONS = {'suggestions': [
        {'placePrediction': {
            'placeId': 'ChIJkompally000001', 'text': {'text': 'Kompally, Hyderabad, Telangana, India'},
            'structuredFormat': {'mainText': {'text': 'Kompally'},
                                 'secondaryText': {'text': 'Hyderabad, Telangana, India'}},
        }},
        {'queryPrediction': {'text': {'text': 'kompally restaurants'}}},   # not a place → skipped
    ]}

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_suggestions_use_the_places_key_in_a_header(self):
        with patch('venues.geocoding.requests.request', return_value=fake_http(self.SUGGESTIONS)) as net:
            r = self.client.get('/api/geo/autocomplete',
                                {'q': 'kompa', 'session': 'abcdef12-3456', 'lat': '17.5', 'lng': '78.5'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['suggestions'], [{
            'placeId': 'ChIJkompally000001', 'main': 'Kompally',
            'secondary': 'Hyderabad, Telangana, India', 'label': 'Kompally, Hyderabad, Telangana, India',
        }])
        kwargs = net.call_args.kwargs
        self.assertEqual(kwargs['headers']['X-Goog-Api-Key'], 'places-key')
        body = kwargs['json']
        self.assertEqual(body['includedRegionCodes'], ['in'])
        self.assertEqual(body['sessionToken'], 'abcdef12-3456')
        self.assertEqual(body['locationBias']['circle']['center'], {'latitude': 17.5, 'longitude': 78.5})
        self.assertNotIn('places-key', str(r.content))

    @override_settings(GOOGLE_PLACES_API_KEY='')
    def test_the_geocoding_key_is_the_fallback(self):
        with patch('venues.geocoding.requests.request', return_value=fake_http({'suggestions': []})) as net:
            self.client.get('/api/geo/autocomplete', {'q': 'warangal'})
        self.assertEqual(net.call_args.kwargs['headers']['X-Goog-Api-Key'], 'geo-key')

    @override_settings(GEOCODER_PROVIDER='nominatim', GEOCODER_CONTACT_EMAIL='ops@example.com')
    def test_unavailable_without_google(self):
        with patch('venues.geocoding.requests.request') as net:
            r = self.client.get('/api/geo/autocomplete', {'q': 'warangal'})
            net.assert_not_called()
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.data['code'], 'GEOCODER_NOT_CONFIGURED')

    def test_places_errors_are_a_502(self):
        error = {'error': {'code': 403, 'status': 'PERMISSION_DENIED', 'message': 'Places API (New) has not been used'}}
        with patch('venues.geocoding.requests.request', return_value=fake_http(error, 403)), \
                self.assertLogs('venues.geocoding', level='WARNING') as logs:
            r = self.client.get('/api/geo/autocomplete', {'q': 'warangal'})
        self.assertEqual(r.status_code, 502)
        self.assertIn('PERMISSION_DENIED', '\n'.join(logs.output))

    def test_input_validation(self):
        self.assertEqual(self.client.get('/api/geo/autocomplete', {'q': 'a'}).status_code, 400)
        self.assertEqual(self.client.get('/api/geo/autocomplete', {'q': 'ab', 'lat': 'x', 'lng': '1'}).status_code, 400)
        self.assertEqual(self.client.get('/api/geo/place', {'id': 'bad id!'}).status_code, 400)

    def test_place_details_return_the_address_parts(self):
        place = {
            'id': 'ChIJkompally000001',
            'formattedAddress': 'Kompally, Hyderabad, Telangana, India',
            'location': {'latitude': 17.5354, 'longitude': 78.4836},
            'addressComponents': [
                {'longText': 'Kompally', 'shortText': 'Kompally', 'types': ['sublocality_level_1']},
                {'longText': 'Hyderabad', 'shortText': 'Hyderabad', 'types': ['locality']},
                {'longText': 'Medchal–Malkajgiri', 'shortText': 'Medchal–Malkajgiri',
                 'types': ['administrative_area_level_3']},
                {'longText': 'Telangana', 'shortText': 'TG', 'types': ['administrative_area_level_1']},
            ],
        }
        with patch('venues.geocoding.requests.request', return_value=fake_http(place)) as net:
            r = self.client.get('/api/geo/place', {'id': 'ChIJkompally000001', 'session': 'abcdef12-3456'})
        self.assertEqual(net.call_args.kwargs['headers']['X-Goog-FieldMask'],
                         'id,formattedAddress,location,addressComponents')
        self.assertEqual(net.call_args.kwargs['params']['sessionToken'], 'abcdef12-3456')
        result = r.data['result']
        self.assertEqual((result['locality'], result['city'], result['district'], result['state']),
                         ('Kompally', 'Hyderabad', 'Medchal Malkajgiri', 'Telangana'))
        self.assertEqual((result['lat'], result['lng']), (17.5354, 78.4836))
        self.assertEqual(result['formattedAddress'], 'Kompally, Hyderabad, Telangana, India')


class RegionListTests(APITestCase):
    def test_regions_need_no_provider(self):
        cache.clear()
        r = self.client.get('/api/geo/regions')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['country'], 'India')
        self.assertEqual(len(r.data['states']), 36)
        telangana = next(s for s in r.data['states'] if s['name'] == 'Telangana')
        self.assertIn('Warangal', telangana['districts'])
        self.assertEqual(telangana['type'], 'state')


from .models import Listing  # noqa: E402
from .tests import FULL_DATA, DraftTestBase  # noqa: E402

# Vendor scenarios: a complete draft in each state is accepted by the REAL
# submit endpoint, and the published listing gets its distance pin from the
# vendor's confirmed map location when the maps link has none.
PAN_INDIA = (
    ('Telangana', 'Medchal Malkajgiri', 'Kompally', '500100', 17.5354, 78.4836),
    ('Karnataka', 'Bengaluru Urban', 'Bengaluru', '560034', 12.9352, 77.6245),
    ('Maharashtra', 'Pune', 'Pune', '411001', 18.5204, 73.8567),
    ('Kerala', 'Ernakulam', 'Kochi', '682001', 9.9312, 76.2673),
    ('Assam', 'Kamrup Metro', 'Guwahati', '781001', 26.1445, 91.7362),
)


class PanIndiaSubmitTests(DraftTestBase):
    def draft_in(self, state, district, city, pincode, lat, lng):
        data = {key: dict(value) if isinstance(value, dict) else value for key, value in FULL_DATA.items()}
        data['location'] = {
            'country': 'India', 'houseStreet': '1 Main Road', 'stateName': state, 'district': district,
            'city': city, 'pincode': pincode, 'mapsLink': 'https://maps.app.goo.gl/NoPinHere',
            'latitude': str(lat), 'longitude': str(lng),
        }
        return VenueDraft.objects.create(vendor=self.vendor, data=data)

    def test_a_vendor_can_register_in_any_state(self):
        for state, district, city, pincode, lat, lng in PAN_INDIA:
            draft = self.draft_in(state, district, city, pincode, lat, lng)
            response = self.client.post(f'/api/venues/drafts/{draft.id}/submit')
            self.assertEqual(response.status_code, 200, (state, response.data))
            self.assertEqual(response.data['status'], 'pending')

    def test_the_wrong_district_or_pin_for_the_state_blocks_submit(self):
        draft = self.draft_in('Karnataka', 'Pune', 'Bengaluru', '560034', 12.9, 77.6)
        response = self.client.post(f'/api/venues/drafts/{draft.id}/submit')
        self.assertEqual(response.status_code, 400)
        self.assertIn('District (choose a district in Karnataka)', response.data['message'])
        draft = self.draft_in('Maharashtra', 'Pune', 'Pune', '500084', 18.5, 73.8)
        response = self.client.post(f'/api/venues/drafts/{draft.id}/submit')
        self.assertEqual(response.status_code, 400)
        self.assertIn('valid pincode', response.data['message'])

    def test_autosave_rejects_a_pin_outside_india(self):
        draft = self.draft_in(*PAN_INDIA[0])
        response = self.client.patch(f'/api/venues/drafts/{draft.id}/sections/location',
                                     {'latitude': 51.5, 'longitude': -0.12}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_published_listing_takes_the_confirmed_map_pin(self):
        state, district, city, pincode, lat, lng = PAN_INDIA[2]
        draft = self.draft_in(state, district, city, pincode, lat, lng)
        response = self.client.post('/api/vendors/me/listings', {
            'id': str(draft.id), 'name': 'Pune Hall', 'category': 'Party hall', 'city': city,
            'district': district, 'state': state,
            'detail': {'mapsLink': 'https://maps.app.goo.gl/NoPinHere', 'mapPin': {'lat': lat, 'lng': lng}},
        }, format='json')
        self.assertIn(response.status_code, (200, 201), response.data)
        listing = Listing.objects.get(pk=draft.id)
        self.assertEqual((listing.latitude, listing.longitude), (lat, lng))
        # …so a customer in Pune finds it within 30 km, and one in Hyderabad does not.
        self.client.force_authenticate(user=None)
        listing.status = Listing.Status.LIVE
        listing.save(update_fields=['status'])
        near = self.client.get('/api/venues', {'lat': 18.53, 'lng': 73.85, 'radius': 30})
        far = self.client.get('/api/venues', {'lat': 17.385, 'lng': 78.487, 'radius': 30})
        self.assertEqual([v['name'] for v in near.data['venues']], ['Pune Hall'])
        self.assertEqual(far.data['venues'], [])
