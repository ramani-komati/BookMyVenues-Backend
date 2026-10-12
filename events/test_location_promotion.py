"""
Event location discovery, location display and homepage promotion.

Location: a published event must be found by the place it is actually in,
however the organiser typed it (Hanamkonda / Hanumakonda, packed into the
address line, city "Hanamkonda" vs city "Warangal"), and must NOT turn up
for a place it is not in. Drafts and unpublished events never surface.

Promotion: the super admin promotes/unpromotes PUBLISHED events, separate
from publishing, capped at Event.MAX_FEATURED, and an event that leaves
LIVE loses its promotion so it can never be featured publicly.
"""
import datetime

from django.urls import reverse
from rest_framework.test import APIClient

from bookings.slots import today_ist

from .models import Event, EventOrder
from .tests import EventTestCase, admin_client, make_admin, make_customer, make_event


def place_event(name, *, venue_name='Open Grounds', address='', locality='',
                city='', maps_link='', category='Dandiya', **kwargs):
    """make_event with the place fields set exactly as an organiser typed them."""
    event = make_event(name=name, **kwargs)
    Event.objects.filter(pk=event.pk).update(
        venue_name=venue_name, address=address, locality=locality, city=city,
        maps_link=maps_link, category=category,
    )
    event.refresh_from_db()
    return event


class EventLocationSearchTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.hanamkonda = place_event(
            'ABC Dandiya Night', venue_name='Hanamkonda Open Grounds',
            address='Beside the stadium, Hunter Road', locality='Hanamkonda',
            city='Hanamkonda',
        )
        self.kazipet = place_event(
            'Kazipet Garba', venue_name='Railway Grounds', locality='Kazipet',
            city='Warangal', category='Garba Night',
        )
        self.hyderabad = place_event(
            'Hitech Concert', venue_name='HITEX', locality='Madhapur',
            city='Hyderabad', category='Concert',
        )

    def names(self, **params):
        response = self.client.get(reverse('public-event-list'), params)
        self.assertEqual(response.status_code, 200)
        return sorted(e['name'] for e in response.data['events'])

    def test_a_hanamkonda_event_is_found_by_searching_warangal(self):
        self.assertIn('ABC Dandiya Night', self.names(q='Warangal'))
        self.assertIn('ABC Dandiya Night', self.names(city='Warangal'))

    def test_the_warangal_area_does_not_include_other_cities(self):
        self.assertEqual(
            self.names(q='Warangal'), ['ABC Dandiya Night', 'Kazipet Garba']
        )
        self.assertEqual(self.names(city='Hyderabad'), ['Hitech Concert'])

    def test_a_locality_only_matches_events_in_that_locality(self):
        self.assertEqual(self.names(q='Kazipet'), ['Kazipet Garba'])
        self.assertEqual(self.names(locality='Hanamkonda'), ['ABC Dandiya Night'])

    def test_alternative_spellings_match_each_other(self):
        self.assertEqual(self.names(q='Hanumakonda'), ['ABC Dandiya Night'])
        self.assertEqual(self.names(locality='hanumakonda'), ['ABC Dandiya Night'])

    def test_a_place_typed_only_in_the_address_is_found(self):
        place_event(
            'Fort Navaratri', venue_name='Fort Grounds',
            address='Fort Road, Hanamkonda, Warangal 506001',
        )
        self.assertIn('Fort Navaratri', self.names(q='Hanamkonda'))
        self.assertIn('Fort Navaratri', self.names(city='Warangal'))

    def test_every_search_word_must_match(self):
        self.assertEqual(self.names(q='dandiya warangal'), ['ABC Dandiya Night'])
        self.assertEqual(self.names(q='dandiya hyderabad'), [])

    def test_drafts_unpublished_and_rejected_events_stay_hidden(self):
        for status in (Event.Status.DRAFT, Event.Status.PAUSED,
                       Event.Status.REJECTED, Event.Status.PENDING,
                       Event.Status.DELETED):
            place_event(f'Hidden {status}', locality='Hanamkonda',
                        city='Warangal', status=status)
        self.assertEqual(
            self.names(q='Warangal'), ['ABC Dandiya Night', 'Kazipet Garba']
        )

    def test_an_event_with_many_dates_is_listed_once(self):
        place_event('Nine Nights', locality='Hanamkonda', city='Warangal',
                    days_ahead=range(1, 10))
        names = self.names(q='Warangal')
        self.assertEqual(names.count('Nine Nights'), 1)


class EventLocationDisplayTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()

    def test_the_detail_carries_the_full_address_without_repeats(self):
        event = place_event(
            'ABC Dandiya Night', venue_name='Hanamkonda Open Grounds',
            address='Beside the stadium, Hunter Road, Warangal',
            locality='Hanamkonda', city='Warangal',
            maps_link='https://maps.app.goo.gl/abc123',
        )
        data = self.client.get(
            reverse('public-event-detail', args=[event.slug])
        ).data
        self.assertEqual(data['venueName'], 'Hanamkonda Open Grounds')
        self.assertEqual(data['locality'], 'Hanamkonda')
        self.assertEqual(data['city'], 'Warangal')
        self.assertEqual(data['mapsLink'], 'https://maps.app.goo.gl/abc123')
        self.assertEqual(
            data['fullAddress'],
            'Hanamkonda Open Grounds, Beside the stadium, Hunter Road, '
            'Warangal, Hanamkonda',
        )
        # The card line stays short: venue, locality, city.
        self.assertEqual(
            data['location'], 'Hanamkonda Open Grounds, Hanamkonda, Warangal'
        )

    def test_a_ticket_shows_the_events_full_address(self):
        event = place_event(
            'ABC Dandiya Night', venue_name='Open Grounds',
            address='Hunter Road', locality='Hanamkonda', city='Warangal',
        )
        customer = make_customer()
        EventOrder.objects.create(
            event=event, event_date=event.dates.first(), user=customer,
            event_name=event.name, location='Warangal',   # an old, thin snapshot
            date=event.dates.first().date, status='confirmed',
        )
        record = EventOrder.objects.get(user=customer).as_record()
        self.assertEqual(record['address'], 'Open Grounds, Hunter Road, Hanamkonda, Warangal')
        self.assertEqual(record['location'], 'Warangal')   # the snapshot is untouched


class MapsLinkValidationTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.client = admin_client(make_admin())
        self.event = make_event(name='Maps Night')

    def patch(self, link):
        return self.client.patch(
            reverse('admin-event', args=[self.event.pk]), {'mapsLink': link},
            format='json',
        )

    def test_google_maps_links_are_accepted(self):
        for link in (
            'https://maps.app.goo.gl/AbC123',
            'https://www.google.com/maps/place/Hanamkonda/@17.99,79.56,15z',
            'https://www.google.co.in/maps?q=17.99,79.56',
            'https://maps.google.com/?q=Hanamkonda',
            '',
        ):
            response = self.patch(link)
            self.assertEqual(response.status_code, 200, link)
            self.event.refresh_from_db()
            self.assertEqual(self.event.maps_link, link)

    def test_other_links_are_refused_and_the_old_link_kept(self):
        self.patch('https://maps.app.goo.gl/keep')
        for link in (
            'https://evil.example.com/maps',
            'javascript:alert(1)',
            'https://www.google.com/search?q=x',
            'https://google.com.evil.example/maps',
            'https://www.google.com.evil.example/maps/place/x',
            'ftp://maps.google.com/x',
        ):
            response = self.patch(link)
            self.assertEqual(response.status_code, 400, link)
        self.event.refresh_from_db()
        self.assertEqual(self.event.maps_link, 'https://maps.app.goo.gl/keep')


class EventPromotionTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.admin_client = admin_client(make_admin())
        self.public = APIClient()

    def promote(self, event, on=True):
        return self.admin_client.patch(
            reverse('admin-event', args=[event.pk]), {'featured': on}, format='json'
        )

    def featured_names(self):
        # The public list is cache_page'd for 60s; a promotion change shows
        # within that window in production. Tests read the fresh answer.
        from django.core.cache import cache
        cache.clear()
        response = self.public.get(reverse('public-event-list'), {'featured': '1'})
        self.assertEqual(response.status_code, 200)
        return [e['name'] for e in response.data['events']]

    def test_a_published_event_can_be_promoted_and_unpromoted(self):
        event = make_event(name='Promo Night')
        response = self.promote(event)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['event']['featured'])
        self.assertEqual(self.featured_names(), ['Promo Night'])

        response = self.promote(event, on=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['event']['featured'])
        self.assertEqual(self.featured_names(), [])
        # Unpromoting never touches publishing or deletes anything.
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.LIVE)
        self.assertIn('Promo Night', [
            e['name'] for e in self.public.get(reverse('public-event-list')).data['events']
        ])

    def test_promotion_is_separate_from_publishing(self):
        event = make_event(name='Promo Night')
        self.promote(event)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.LIVE)   # unchanged

    def test_an_unpublished_event_cannot_be_promoted(self):
        for status in (Event.Status.DRAFT, Event.Status.PAUSED,
                       Event.Status.REJECTED, Event.Status.PENDING):
            event = make_event(name=f'Not live {status}', status=status)
            response = self.promote(event)
            self.assertEqual(response.status_code, 400, status)
            event.refresh_from_db()
            self.assertFalse(event.featured)
        self.assertEqual(self.featured_names(), [])

    def test_unpublishing_drops_the_promotion(self):
        event = make_event(name='Promo Night')
        self.promote(event)
        self.admin_client.patch(
            reverse('admin-event', args=[event.pk]), {'status': 'unpublish'},
            format='json',
        )
        event.refresh_from_db()
        self.assertFalse(event.featured)
        # Republishing does NOT silently re-promote it.
        self.admin_client.patch(
            reverse('admin-event', args=[event.pk]), {'status': 'publish'},
            format='json',
        )
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.LIVE)
        self.assertFalse(event.featured)
        self.assertEqual(self.featured_names(), [])

    def test_deleting_drops_the_promotion(self):
        event = make_event(name='Promo Night')
        self.promote(event)
        self.admin_client.delete(reverse('admin-event', args=[event.pk]))
        event.refresh_from_db()
        self.assertFalse(event.featured)
        self.assertEqual(self.featured_names(), [])

    def test_a_featured_row_can_never_hold_a_non_live_event(self):
        # Even a direct write that forgets the rule is corrected on save.
        event = make_event(name='Promo Night')
        self.promote(event)
        event.refresh_from_db()
        self.assertTrue(event.featured)
        event.status = Event.Status.PAUSED
        event.save(update_fields=['status'])
        event.refresh_from_db()
        self.assertFalse(event.featured)

    def test_at_most_six_events_can_be_promoted(self):
        events = [make_event(name=f'Night {i}') for i in range(Event.MAX_FEATURED + 1)]
        for event in events[:Event.MAX_FEATURED]:
            self.assertEqual(self.promote(event).status_code, 200)
        response = self.promote(events[-1])
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'PROMOTION_LIMIT')
        events[-1].refresh_from_db()
        self.assertFalse(events[-1].featured)
        # Freeing a slot makes room again.
        self.promote(events[0], on=False)
        self.assertEqual(self.promote(events[-1]).status_code, 200)

    def test_re_promoting_an_already_promoted_event_is_not_counted_twice(self):
        events = [make_event(name=f'Night {i}') for i in range(Event.MAX_FEATURED)]
        for event in events:
            self.promote(event)
        self.assertEqual(self.promote(events[0]).status_code, 200)

    def test_featured_must_be_a_real_boolean(self):
        event = make_event(name='Promo Night')
        response = self.promote(event, on='false')
        self.assertEqual(response.status_code, 400)
        event.refresh_from_db()
        self.assertFalse(event.featured)

    def test_a_promoted_event_with_no_upcoming_date_leaves_the_row(self):
        event = make_event(name='Promo Night')
        self.promote(event)
        event.dates.update(date=today_ist() - datetime.timedelta(days=2))
        self.assertEqual(self.featured_names(), [])

    def test_promoted_events_are_ordered_newest_promotion_first(self):
        first = make_event(name='First')
        second = make_event(name='Second')
        self.promote(first)
        self.promote(second)
        self.assertEqual(self.featured_names(), ['Second', 'First'])

    def test_promotion_changes_are_audited(self):
        from adminpanel.models import AuditEntry

        event = make_event(name='Promo Night')
        self.promote(event)
        self.promote(event, on=False)
        actions = list(
            AuditEntry.objects.filter(target_id=str(event.pk))
            .values_list('action', flat=True)
        )
        self.assertIn('Promoted event', actions)
        self.assertIn('Removed event promotion', actions)
        self.assertNotIn('Edited event', actions)
