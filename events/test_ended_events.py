"""
QA audit 2026-10-30: ended events stayed live. They now move to `ended` by
themselves — hidden from the list, not bookable, orders untouched, and the
detail page still opens (marked ended) for people holding the link.
"""
import datetime
import uuid

from django.core.cache import cache
from django.core.management import call_command
from rest_framework.test import APITestCase

from accounts.models import User
from bookings.slots import IST

from .lifecycle import archive_ended_events, event_end
from .models import Event, EventDate, EventOrder


def at(day, hh, mm=0):
    return datetime.datetime.combine(day, datetime.time(hh, mm), tzinfo=IST)


class EndedEventTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.today = datetime.date(2026, 10, 30)

    def event(self, name, dates, start=(18, 0), end=(22, 0), status=Event.Status.LIVE, featured=False):
        event = Event.objects.create(
            name=name, slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:4]}',
            start_time=datetime.time(*start), end_time=datetime.time(*end), status=status,
            featured=featured,
        )
        for day in dates:
            EventDate.objects.create(event=event, date=day)
        return event

    def test_end_moment_handles_overnight_shows(self):
        day = self.today
        self.assertEqual(event_end(day, datetime.time(18), datetime.time(22)), at(day, 22))
        self.assertEqual(event_end(day, datetime.time(20), datetime.time(1)),
                         at(day + datetime.timedelta(days=1), 1))

    def test_only_events_whose_last_show_is_over_move(self):
        yesterday = self.event('Past', [self.today - datetime.timedelta(days=1)])
        tonight = self.event('Tonight', [self.today])
        running = self.event('Multi night', [self.today - datetime.timedelta(days=3), self.today + datetime.timedelta(days=2)])
        overnight = self.event('Late show', [self.today - datetime.timedelta(days=1)], start=(21, 0), end=(2, 0))
        undated = self.event('No dates', [])
        paused = self.event('Paused past', [self.today - datetime.timedelta(days=5)], status=Event.Status.PAUSED)

        moved = archive_ended_events(now=at(self.today, 1, 30))  # 1:30 AM today
        self.assertEqual(moved, 1)                                  # only 'Past'
        for e in (yesterday, tonight, running, overnight, undated, paused):
            e.refresh_from_db()
        self.assertEqual(yesterday.status, Event.Status.ENDED)
        self.assertEqual(overnight.status, Event.Status.LIVE)       # still on until 2 AM
        self.assertEqual(tonight.status, Event.Status.LIVE)
        self.assertEqual(running.status, Event.Status.LIVE)
        self.assertEqual(undated.status, Event.Status.LIVE)
        self.assertEqual(paused.status, Event.Status.PAUSED)        # admin choice untouched

        archive_ended_events(now=at(self.today, 22, 30))
        tonight.refresh_from_db()
        overnight.refresh_from_db()
        self.assertEqual(tonight.status, Event.Status.ENDED)
        self.assertEqual(overnight.status, Event.Status.ENDED)

    def test_ended_event_keeps_orders_drops_promotion_and_leaves_the_list(self):
        event = self.event('Over', [datetime.date(2020, 1, 1)], featured=True)
        user = User.objects.create_user(phone='9555500001', name='Buyer')
        order = EventOrder.objects.create(event=event, event_date=event.dates.first(), user=user,
                                          event_name='Over', date=datetime.date(2020, 1, 1),
                                          status='confirmed', amount=100)
        response = self.client.get('/api/events')                   # sweeps first
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Over', str(response.data))
        event.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(event.status, Event.Status.ENDED)
        self.assertFalse(event.featured)
        self.assertEqual(order.status, 'confirmed')                 # history preserved
        detail = self.client.get(f'/api/events/{event.slug}')
        self.assertEqual(detail.status_code, 200)                   # shared link still opens
        self.assertTrue(detail.data['ended'])

    def test_management_command(self):
        self.event('Old', [datetime.date(2020, 1, 1)])
        call_command('archive_ended_events', stdout=open('NUL' if __import__('os').name == 'nt' else '/dev/null', 'w'))
        self.assertTrue(Event.objects.filter(status=Event.Status.ENDED).exists())
