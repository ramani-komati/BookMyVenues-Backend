"""
Event map pins and "near you" discovery (2026-11-12).

The bug: an admin published a Warangal event with a Google Maps SHARE link
(maps.app.goo.gl/…). A share link carries no coordinates, events never
followed it, so the event had no pin — and every location-aware list (the
Events page's 30 km radius and the homepage's Top events) dropped it for a
customer standing in Warangal.

Fixed two ways, both covered here:
  1. Saving an event follows its share link once (Google hosts only) and
     keeps {source, url}; the pin is read from it while `source` is still
     the current link. Best-effort — a failure never blocks the save.
  2. ?place=<city> with a radius also returns events WITHOUT a pin that are
     named as being in that place, as `distanceUnknown` (never with an
     invented distance).
"""
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from venues.maps import MapsError

from .models import Event
from .test_location_promotion import place_event
from .tests import EventTestCase, admin_client, make_admin, make_event

SHORT = 'https://maps.app.goo.gl/mWqnYgsjCMNeE6LP7'
# What that share link redirects to (an exact pin in Warangal).
RESOLVED = (
    'https://www.google.com/maps/place/Old+Walmart+Ground/@17.9661659,79.5985681,19z/'
    'data=!4m7!3m6!1s0x3a3345f0ecfb1def:0x15f51305cf1b305d!8m2!3d17.9661656!4d79.5993208'
)
WARANGAL = {'lat': 17.9689, 'lng': 79.5941}
HYDERABAD = {'lat': 17.3850, 'lng': 78.4867}


@override_settings(EVENT_RESOLVE_MAPS_LINKS=True)
class EventShortLinkPinTests(EventTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = admin_client(make_admin())
        self.event = make_event(name='Halloween Carnival')

    def patch(self, body):
        return self.client.patch(
            reverse('admin-event', args=[self.event.pk]), body, format='json',
        )

    def test_a_share_link_is_followed_and_gives_the_event_its_pin(self):
        with mock.patch('venues.maps.resolve_short_link', return_value=RESOLVED) as resolve:
            response = self.patch({'mapsLink': SHORT, 'city': 'Warangal'})
        self.assertEqual(response.status_code, 200)
        resolve.assert_called_once_with(SHORT)
        self.event.refresh_from_db()
        self.assertEqual(self.event.maps_link, SHORT)       # admin's link untouched
        self.assertAlmostEqual(self.event.latitude, 17.9661656)
        self.assertAlmostEqual(self.event.longitude, 79.5993208)
        self.assertEqual(self.event.maps_link_resolved, {'source': SHORT, 'url': RESOLVED})

    def test_an_unrelated_edit_does_not_fetch_the_link_again(self):
        with mock.patch('venues.maps.resolve_short_link', return_value=RESOLVED):
            self.patch({'mapsLink': SHORT})
        with mock.patch('venues.maps.resolve_short_link') as resolve:
            self.assertEqual(self.patch({'name': 'Halloween Carnival 2'}).status_code, 200)
        resolve.assert_not_called()
        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.latitude, 17.9661656)

    def test_re_saving_an_older_unpinned_event_fills_its_pin(self):
        # Saved before events followed share links: link stored, no pin.
        Event.objects.filter(pk=self.event.pk).update(maps_link=SHORT, latitude=None, longitude=None)
        with mock.patch('venues.maps.resolve_short_link', return_value=RESOLVED):
            self.assertEqual(self.patch({'description': 'Now with a pin'}).status_code, 200)
        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.latitude, 17.9661656)

    def test_a_link_that_cannot_be_followed_never_blocks_the_save(self):
        for failure in (MapsError('Google said no'), ConnectionError('offline')):
            with mock.patch('venues.maps.resolve_short_link', side_effect=failure):
                response = self.patch({'mapsLink': SHORT, 'name': f'Saved {failure}'})
            self.assertEqual(response.status_code, 200, failure)
            self.event.refresh_from_db()
            self.assertEqual(self.event.maps_link, SHORT)
            self.assertIsNone(self.event.latitude)

    def test_replacing_the_link_never_keeps_the_old_pin(self):
        with mock.patch('venues.maps.resolve_short_link', return_value=RESOLVED):
            self.patch({'mapsLink': SHORT})
        with mock.patch('venues.maps.resolve_short_link', side_effect=MapsError('no')):
            self.patch({'mapsLink': 'https://maps.app.goo.gl/SomewhereElse'})
        self.event.refresh_from_db()
        self.assertIsNone(self.event.latitude)
        # A full link with its own pin wins, and the stale resolution goes.
        self.patch({'mapsLink': 'https://www.google.com/maps?q=17.99,79.56'})
        self.event.refresh_from_db()
        self.assertAlmostEqual(self.event.latitude, 17.99)
        self.assertEqual(self.event.maps_link_resolved, {})

    def test_vendor_saves_follow_share_links_too(self):
        # Same shared write path (events/writes.py apply_core_fields).
        from .writes import apply_core_fields
        with mock.patch('venues.maps.resolve_short_link', return_value=RESOLVED):
            apply_core_fields(self.event, {'mapsLink': SHORT}, creating=False)
        self.event.save()
        self.assertAlmostEqual(self.event.latitude, 17.9661656)


class EventShortLinkSafetyTests(EventTestCase):
    def test_the_test_suite_never_reaches_google(self):
        client = admin_client(make_admin())
        event = make_event(name='Offline')
        with mock.patch('venues.maps.resolve_short_link', side_effect=AssertionError('network')) as resolve:
            response = client.patch(
                reverse('admin-event', args=[event.pk]), {'mapsLink': SHORT}, format='json',
            )
        self.assertEqual(response.status_code, 200)
        resolve.assert_not_called()


class NearbyEventTests(EventTestCase):
    """A customer who picked Warangal sees the Warangal events — pinned ones
    by distance, unpinned ones by place name — and nothing from elsewhere."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.pinned = place_event(
            'Halloween Carnival', venue_name='Old Walmart Ground', city='Warangal',
            maps_link=RESOLVED,
        )
        Event.objects.filter(pk=self.pinned.pk).update(latitude=17.9661656, longitude=79.5993208)
        self.unpinned = place_event(
            'Hanamkonda Dandiya', venue_name='Open Grounds', city='Hanamkonda',
        )
        self.elsewhere = place_event(
            'Hitech Concert', venue_name='HITEX', city='Hyderabad', category='Concert',
        )

    def get(self, **params):
        response = self.client.get(reverse('public-event-list'), params)
        self.assertEqual(response.status_code, 200)
        return response.data

    def test_a_pinned_warangal_event_is_within_30_km_of_warangal(self):
        data = self.get(radius=30, **WARANGAL)
        self.assertEqual([e['name'] for e in data['events']], ['Halloween Carnival'])
        self.assertLess(data['events'][0]['distanceKm'], 2)
        self.assertNotIn('distanceUnknown', data)      # unchanged without ?place=
        self.assertEqual(self.get(radius=30, **HYDERABAD)['events'], [])

    def test_unpinned_events_in_the_customers_place_come_back_separately(self):
        data = self.get(radius=30, place='Warangal', **WARANGAL)
        self.assertEqual([e['name'] for e in data['events']], ['Halloween Carnival'])
        unknown = data['distanceUnknown']
        # Warangal covers Hanamkonda (events/location.py), so it is found.
        self.assertEqual([e['name'] for e in unknown], ['Hanamkonda Dandiya'])
        self.assertIsNone(unknown[0]['distanceKm'])
        self.assertTrue(unknown[0]['distanceUnknown'])
        self.assertEqual(data['distanceUnknownTotal'], 1)

    def test_other_places_do_not_get_them(self):
        data = self.get(radius=30, place='Hyderabad', **HYDERABAD)
        self.assertEqual(data['events'], [])
        # Only Hyderabad's own unpinned event — nothing from Warangal.
        self.assertEqual([e['name'] for e in data['distanceUnknown']], ['Hitech Concert'])
        data = self.get(radius=30, place='Kazipet', **WARANGAL)
        self.assertEqual(data['distanceUnknown'], [])     # Hanamkonda is not in Kazipet

    def test_drafts_never_surface_through_the_place_fallback(self):
        place_event('Hidden draft', city='Warangal', status=Event.Status.DRAFT)
        place_event('Hidden pending', city='Warangal', status=Event.Status.PENDING)
        names = [e['name'] for e in self.get(radius=30, place='Warangal', **WARANGAL)['distanceUnknown']]
        self.assertEqual(names, ['Hanamkonda Dandiya'])

    def test_the_featured_row_uses_the_same_rules(self):
        Event.objects.filter(pk__in=[self.pinned.pk, self.unpinned.pk, self.elsewhere.pk]).update(featured=True)
        data = self.get(featured=1, radius=30, place='Warangal', **WARANGAL)
        self.assertEqual([e['name'] for e in data['events']], ['Halloween Carnival'])
        self.assertEqual([e['name'] for e in data['distanceUnknown']], ['Hanamkonda Dandiya'])


class ListingCoordinatesEventTests(EventTestCase):
    def test_the_command_pins_existing_events_from_their_share_links(self):
        event = make_event(name='Halloween Carnival')
        Event.objects.filter(pk=event.pk).update(maps_link=SHORT, latitude=None, longitude=None)
        out = StringIO()
        with mock.patch(
            'venues.management.commands.listing_coordinates.resolve_short_link', return_value=RESOLVED,
        ):
            call_command('listing_coordinates', '--resolve-short-links', stdout=out)
            event.refresh_from_db()
            self.assertIsNone(event.latitude)                 # dry run writes nothing
            self.assertIn('would pin at 17.966166, 79.599321', out.getvalue())
            call_command('listing_coordinates', '--resolve-short-links', '--apply', stdout=out)
        event.refresh_from_db()
        self.assertAlmostEqual(event.latitude, 17.9661656)
        self.assertEqual(event.maps_link, SHORT)
        self.assertIn('1 filled from short links', out.getvalue())
