"""
QA audit 2026-10-30 — location and venue discovery.

  * venues without a pin are never silently dropped from a radius search:
    ?includeUnmapped=1 returns them separately as "distance unknown";
  * list rows carry the card's offers (no per-card detail call);
  * more Google Maps link formats yield an exact pin, and a pinless place
    link yields its place name — never a guessed point;
  * /api/maps/resolve accepts full Maps URLs without a network call, returns
    the pin, and caches failures;
  * a Google refusal (billing off) pauses Google instead of hammering it, and
    search falls back to OpenStreetMap when that is configured;
  * new venue submissions need a usable map location.
"""
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.models import User

from .geo import pin_from_maps_link, place_text_from_maps_link
from .models import Listing

PIN_LINK = 'https://www.google.com/maps/place/X/data=!3d17.9683809!4d79.5987069'


def make_listing(vendor, name, *, link='', status=Listing.Status.LIVE, category='Play zone',
                 offers=None):
    detail = {'mapsLink': link}
    if offers is not None:
        detail['offers'] = offers
    return Listing.objects.create(
        id=uuid.uuid4(), vendor=vendor, slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:6]}',
        record={'name': name, 'price': 500, 'category': category, 'detail': detail},
        name=name, category=category, locality='Hanamkonda', pincode='506001', status=status,
    )


class DistanceUnknownListTests(APITestCase):
    URL = '/api/venues'

    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(
            phone='9611100001', name='V', email='dv@example.com', role=User.Role.VENDOR,
        )
        self.near = make_listing(self.vendor, 'Near Pinned', link=PIN_LINK)
        self.unpinned = make_listing(self.vendor, 'No Pin Venue', link='https://maps.app.goo.gl/abc')
        self.far = make_listing(
            self.vendor, 'Far Pinned',
            link='https://www.google.com/maps/place/Y/data=!3d28.61!4d77.20',  # Delhi
        )

    def get(self, **params):
        return self.client.get(self.URL, {'lat': 17.97, 'lng': 79.59, 'radius': 30, **params})

    def test_radius_without_flag_keeps_old_contract(self):
        data = self.get().data
        self.assertEqual([v['name'] for v in data['venues']], ['Near Pinned'])
        self.assertNotIn('distanceUnknown', data)

    def test_unpinned_venues_come_back_separately(self):
        data = self.get(includeUnmapped=1).data
        self.assertEqual([v['name'] for v in data['venues']], ['Near Pinned'])
        self.assertEqual(data['total'], 1)
        self.assertEqual([v['name'] for v in data['distanceUnknown']], ['No Pin Venue'])
        self.assertEqual(data['distanceUnknownTotal'], 1)
        row = data['distanceUnknown'][0]
        self.assertIsNone(row['distanceKm'])          # never an invented distance
        self.assertIsNone(row['coordinates'])
        self.assertTrue(row['distanceUnknown'])

    def test_unknown_section_honours_the_category_filter(self):
        make_listing(self.vendor, 'Unpinned Theatre', link='', category='Private theatre')
        data = self.get(includeUnmapped=1, category='Private theatre').data
        self.assertEqual([v['name'] for v in data['distanceUnknown']], ['Unpinned Theatre'])
        self.assertEqual(data['venues'], [])

    def test_hidden_venues_never_appear_as_unknown(self):
        make_listing(self.vendor, 'Paused', status=Listing.Status.PAUSED)
        names = [v['name'] for v in self.get(includeUnmapped=1).data['distanceUnknown']]
        self.assertNotIn('Paused', names)

    def test_without_radius_unpinned_are_already_in_the_list(self):
        data = self.client.get(self.URL, {
            'lat': 17.97, 'lng': 79.59, 'sort': 'distance', 'includeUnmapped': 1,
        }).data
        names = [v['name'] for v in data['venues']]
        self.assertEqual(names[-1], 'No Pin Venue')   # pinless go last
        self.assertEqual(data['distanceUnknown'], [])

    def test_list_rows_carry_card_offers(self):
        offers = [{'title': f'Offer {i}', 'type': 'percent', 'value': '10'} for i in range(5)]
        offers.append({'title': 'Off', 'enabled': False})
        make_listing(self.vendor, 'Offer Venue', link=PIN_LINK, offers=offers)
        rows = {v['name']: v for v in self.client.get(self.URL).data['venues']}
        self.assertEqual(len(rows['Offer Venue']['offers']), 3)
        self.assertEqual(rows['Near Pinned']['offers'], [])
        self.assertNotIn('detail', rows['Offer Venue'])


class MapsLinkFormatTests(SimpleTestCase):
    def test_exact_formats(self):
        cases = {
            'https://www.google.com/maps/search/17.9689,+79.5941': (17.9689, 79.5941),
            'https://www.google.com/maps/dir/Home/17.9689,79.5941': (17.9689, 79.5941),
            'https://maps.google.com/?q=loc:17.9,79.5': (17.9, 79.5),
            "https://www.google.com/maps/place/17%C2%B058'08.0%22N+79%C2%B035'38.8%22E/@1,2,17z":
                (17.9688889, 79.5941111),
            PIN_LINK: (17.9683809, 79.5987069),
        }
        for link, expected in cases.items():
            self.assertEqual(pin_from_maps_link(link), expected, link)

    def test_viewport_and_names_are_never_a_pin(self):
        for link in (
            'https://www.google.com/maps/place/Foo/@17.9,79.5,17z',
            'https://www.google.com/maps/place/Stayfit/data=!4m2!3m1!1s0x3a33:0x4ce7',
            'https://maps.app.goo.gl/BKEP2ax5yc7AdfCU8',
            'https://www.google.com/maps/search/0,0',
        ):
            self.assertIsNone(pin_from_maps_link(link), link)

    def test_place_name_from_a_pinless_link(self):
        link = ('https://www.google.com/maps/place/Stayfit+Sports+Zone,+Hanamkonda/'
                'data=!4m2!3m1!1s0x3a33:0x4ce7')
        self.assertEqual(place_text_from_maps_link(link), 'Stayfit Sports Zone, Hanamkonda')
        self.assertEqual(place_text_from_maps_link('https://maps.google.com/?q=17.9,79.5'), '')


class MapsResolveAuditTests(APITestCase):
    URL = '/api/maps/resolve'

    def setUp(self):
        cache.clear()

    def test_full_maps_url_needs_no_network(self):
        with patch('venues.maps.requests.get') as get:
            response = self.client.get(self.URL, {'url': PIN_LINK})
        get.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['coordinates'], {'lat': 17.9683809, 'lng': 79.5987069})
        self.assertTrue(response.data['exact'])

    def test_pinless_place_reports_its_name_not_a_point(self):
        final = 'https://www.google.com/maps/place/Strike+Nets,+Warangal/data=!4m2!3m1!1s0x1:0x2'
        fake = MagicMock(is_redirect=True, headers={'Location': final})
        with patch('venues.maps.requests.get', return_value=fake):
            data = self.client.get(self.URL, {'url': 'https://maps.app.goo.gl/Xw'}).data
        self.assertIsNone(data['coordinates'])
        self.assertFalse(data['exact'])
        self.assertEqual(data['placeName'], 'Strike Nets, Warangal')

    def test_failures_are_cached_and_retried_once(self):
        import requests
        with patch('venues.maps.time.sleep'), \
                patch('venues.maps.requests.get', side_effect=requests.ConnectionError('x')) as get:
            first = self.client.get(self.URL, {'url': 'https://maps.app.goo.gl/Down'})
            second = self.client.get(self.URL, {'url': 'https://maps.app.goo.gl/Down'})
        self.assertEqual(first.status_code, 400)
        self.assertEqual(first.data['code'], 'MAPS_LINK_INVALID')
        self.assertEqual(second.status_code, 400)
        self.assertEqual(get.call_count, 2)  # one retry, then served from the failure cache

    def test_consent_interstitial_is_unwrapped(self):
        consent = ('https://consent.google.com/m?continue='
                   'https%3A%2F%2Fwww.google.com%2Fmaps%2Fplace%2FX%2Fdata%3D!3d17.5!4d79.5')
        fake = MagicMock(is_redirect=True, headers={'Location': consent})
        with patch('venues.maps.requests.get', return_value=fake):
            data = self.client.get(self.URL, {'url': 'https://maps.app.goo.gl/EU'}).data
        self.assertEqual(data['coordinates'], {'lat': 17.5, 'lng': 79.5})

    def test_other_hosts_still_refused(self):
        for url in ('https://evil.example.com/maps', 'http://169.254.169.254/maps', 'ftp://goo.gl/x'):
            self.assertEqual(self.client.get(self.URL, {'url': url}).status_code, 400, url)


def _google_answer(status_text, results=None, http=200):
    response = MagicMock(status_code=http)
    response.json.return_value = {'status': status_text, 'results': results or [],
                                  'error_message': 'You must enable Billing'}
    return response


@override_settings(GEOCODER_PROVIDER='google', GOOGLE_GEOCODING_API_KEY='k-test',
                   GEOCODER_CONTACT_EMAIL='')
class GeocoderBreakerTests(APITestCase):
    URL = '/api/geo/search'

    def setUp(self):
        cache.clear()

    def test_billing_refusal_pauses_google(self):
        with patch('venues.geocoding.requests.get', return_value=_google_answer('REQUEST_DENIED')) as get:
            first = self.client.get(self.URL, {'q': 'Hanamkonda'})
            second = self.client.get(self.URL, {'q': 'Kazipet'})  # a different query
        self.assertEqual(first.status_code, 502)
        self.assertEqual(second.status_code, 502)
        self.assertEqual(get.call_count, 1)  # breaker open — Google not asked again
        self.assertNotIn('Billing', str(first.data))  # reason stays in the server log

    def test_transient_failure_is_retried_once(self):
        ok = _google_answer('OK', [{
            'geometry': {'location': {'lat': 18.0, 'lng': 79.56}},
            'formatted_address': 'Hanamkonda, Telangana',
            'address_components': [
                {'long_name': 'Hanamkonda', 'short_name': 'Hanamkonda', 'types': ['locality']},
                {'long_name': 'Telangana', 'short_name': 'TG', 'types': ['administrative_area_level_1']},
            ],
        }])
        with patch('venues.geocoding.time.sleep'), \
                patch('venues.geocoding.requests.get',
                      side_effect=[_google_answer('', http=503), ok]) as get:
            response = self.client.get(self.URL, {'q': 'Hanamkonda'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(response.data['results'][0]['lat'], 18.0)

    @override_settings(GEOCODER_CONTACT_EMAIL='ops@example.com')
    def test_osm_fallback_when_google_refuses(self):
        osm = MagicMock(status_code=200)
        osm.json.return_value = [{
            'lat': '18.0', 'lon': '79.56', 'name': 'Hanamkonda', 'display_name': 'Hanamkonda, Telangana',
            'address': {'suburb': 'Hanamkonda', 'city': 'Warangal', 'state': 'Telangana'},
        }]

        def fake_get(url, **kwargs):
            return _google_answer('REQUEST_DENIED') if 'googleapis' in url else osm

        with patch('venues.geocoding.time.sleep'), patch('venues.geocoding.requests.get', side_effect=fake_get):
            response = self.client.get(self.URL, {'q': 'Hanamkonda'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['lat'], 18.0)


class ScopedSearchTests(SimpleTestCase):
    def test_other_states_are_dropped_when_local_results_exist(self):
        from .geocoding import scope_results
        rows = [
            {'label': 'Kazipet, Bihar', 'state': 'Bihar', 'district': ''},
            {'label': 'Kazipet, Warangal', 'state': 'Telangana', 'district': 'Hanumakonda'},
        ]
        out = scope_results(rows, 'Telangana')
        self.assertEqual([r['label'] for r in out], ['Kazipet, Warangal'])
        self.assertTrue(out[0]['inScope'])

    def test_nothing_local_keeps_results_marked_out_of_scope(self):
        from .geocoding import scope_results
        out = scope_results([{'label': 'X', 'state': 'Bihar', 'district': ''}], 'Telangana')
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0]['inScope'])

    def test_unscoped_search_is_unchanged(self):
        from .geocoding import scope_results
        rows = [{'label': 'A', 'state': 'Bihar'}]
        self.assertEqual(scope_results(rows), rows)


class SitemapTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(
            phone='9611100009', name='V', email='sm@example.com', role=User.Role.VENDOR,
        )

    @override_settings(PUBLIC_SITE_URL='https://thebookmyvenues.in')
    def test_sitemap_lists_live_venues_by_slug_only(self):
        live = make_listing(self.vendor, 'Live Hall')
        make_listing(self.vendor, 'Hidden Hall', status=Listing.Status.PAUSED)
        unit = make_listing(self.vendor, 'Court 2')
        unit.record = {**unit.record, 'detail': {'unitOf': str(live.pk)}}
        unit.save()
        response = self.client.get('/api/sitemap.xml')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response['Content-Type'].startswith('application/xml'))
        body = response.content.decode()
        self.assertTrue(body.startswith('<?xml'))
        self.assertIn(f'<loc>https://thebookmyvenues.in/venue/{live.slug}</loc>', body)
        self.assertIn('<loc>https://thebookmyvenues.in/home</loc>', body)
        self.assertNotIn('hidden-hall', body)
        self.assertNotIn(unit.slug, body)
        self.assertNotIn('onrender', body)


class PlaceSpellingTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(
            phone='9611100010', name='V', email='ps@example.com', role=User.Role.VENDOR,
        )

    def test_spellings_match_each_other_and_display_once(self):
        for name, locality in (('A Hall', 'Sudhanagar, Hanamakonda'), ('B Hall', 'Subedari, Hanumakonda'),
                               ('C Hall', 'Kishanpura, Hanamkonda')):
            listing = make_listing(self.vendor, name)
            listing.locality = locality
            listing.record = {**listing.record, 'location': locality}
            listing.save()
        for query in ('Hanamkonda', 'hanumakonda', 'Hanamakonda'):
            rows = self.client.get('/api/venues', {'q': query}).data['venues']
            self.assertEqual(len(rows), 3, query)
        cache.clear()
        locations = {v['location'] for v in self.client.get('/api/venues').data['venues']}
        self.assertEqual(locations, {'Sudhanagar, Hanamkonda', 'Subedari, Hanamkonda', 'Kishanpura, Hanamkonda'})
        stored = set(Listing.objects.values_list('locality', flat=True))
        self.assertIn('Sudhanagar, Hanamakonda', stored)   # vendor's data untouched
