"""
The customer's unified booking history (bookings/history.py,
GET /api/users/me/booking-history).

  * venue bookings, memberships and event tickets in ONE list
  * ordered by when the booking was MADE (created_at), newest first — never by
    the visit / event date — whichever product it is
  * same-instant rows keep a fixed order, so a refresh never reshuffles them
  * each item carries the exact record the per-product endpoints serve
  * owner-scoped: one customer never sees another's bookings; each product
    keeps its own visibility rule (never-paid venue attempts hidden, tickets
    shown with their real outcome)
  * pagination never repeats or skips a row
"""
import datetime

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from events.models import Event, EventDate, EventOrder

from .models import Booking
from .slots import today_ist
from .test_booking_codes import make_event_order
from .test_checkin import jwt_client, make_booking, make_vendor, make_venue

TODAY = today_ist()
BASE = timezone.now().replace(microsecond=0) - datetime.timedelta(days=10)
URL = '/api/users/me/booking-history'


def at(hours):
    return BASE + datetime.timedelta(hours=hours)


class BookingHistoryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.vendor = make_vendor('9800000001', 'Ravi')
        self.venue = make_venue(self.vendor, 'Gym Venue')
        self.customer = User.objects.create_user(phone='9800000002', name='Asha')
        self.other = User.objects.create_user(phone='9800000003', name='Bala')
        self.event = Event.objects.create(
            slug='basketball-night', name='Basketball Event', category='Sports', listing=self.venue,
            start_time=datetime.time(18, 0), end_time=datetime.time(22, 0), status=Event.Status.LIVE,
        )
        EventDate.objects.create(event=self.event, date=TODAY, capacity=100)
        self.client = jwt_client(self.customer)

    # -- helpers ----------------------------------------------------------
    def venue_booking(self, made_at, *, user=None, date=TODAY, **extra):
        booking = make_booking(self.venue, user or self.customer, date=date, **extra)
        Booking.objects.filter(pk=booking.pk).update(created_at=made_at)
        return booking

    def ticket(self, made_at, *, user=None, date=TODAY, **extra):
        order = make_event_order(self.event, user or self.customer, date=date, **extra)
        EventOrder.objects.filter(pk=order.pk).update(created_at=made_at)
        return order

    def membership(self, made_at):
        from offerings.models import FitnessPlan, Membership
        plan = FitnessPlan.objects.create(
            listing=self.venue, key='monthly', kind='membership', name='Monthly',
            price=1500, duration_value=1, duration_unit='month',
        )
        booking = self.venue_booking(made_at, kind='membership', slots=[])
        Membership.objects.create(
            booking=booking, plan=plan, listing=self.venue, user=self.customer,
            starts_on=TODAY, ends_on=TODAY + datetime.timedelta(days=30),
        )
        return booking

    def history(self, client=None, **params):
        response = (client or self.client).get(URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def ids(self, data):
        return [(item['type'], item['id']) for item in data['items']]

    # -- the brief's scenarios -------------------------------------------
    def test_only_venue_bookings(self):
        old = self.venue_booking(at(1))
        new = self.venue_booking(at(2))
        data = self.history()
        self.assertEqual(self.ids(data), [('venue', new.id), ('venue', old.id)])
        self.assertEqual(data['total'], 2)

    def test_only_event_bookings(self):
        old = self.ticket(at(1))
        new = self.ticket(at(2))
        self.assertEqual(self.ids(self.history()), [('event', new.id), ('event', old.id)])

    def test_the_brief_example_mixes_both_kinds_newest_first(self):
        resort = self.venue_booking(at(0))           # 27 Oct
        birthday = self.ticket(at(27))               # 28 Oct
        gym = self.venue_booking(at(51))             # 29 Oct
        basketball = self.ticket(at(68))             # 30 Oct
        self.assertEqual(self.ids(self.history()), [
            ('event', basketball.id), ('venue', gym.id), ('event', birthday.id), ('venue', resort.id),
        ])

    def test_newest_event_is_above_older_venue_bookings(self):
        venue = self.venue_booking(at(1))
        event = self.ticket(at(2))
        self.assertEqual(self.ids(self.history())[0], ('event', event.id))
        self.assertEqual(self.ids(self.history())[1], ('venue', venue.id))

    def test_newest_venue_is_above_older_event_bookings(self):
        event = self.ticket(at(1))
        venue = self.venue_booking(at(2))
        self.assertEqual(self.ids(self.history()), [('venue', venue.id), ('event', event.id)])

    def test_order_is_by_booking_time_not_visit_date(self):
        # Booked first, but for a date far in the future…
        future_visit = self.venue_booking(at(1), date=TODAY + datetime.timedelta(days=60))
        # …booked later, for an event happening today.
        today_event = self.ticket(at(2), date=TODAY)
        self.assertEqual(self.ids(self.history()), [('event', today_event.id), ('venue', future_visit.id)])

    def test_identical_timestamps_have_a_fixed_order(self):
        rows = [self.venue_booking(at(5)), self.ticket(at(5)), self.venue_booking(at(5)), self.ticket(at(5))]
        first = self.ids(self.history())
        self.assertEqual(len(first), 4)
        # venue bookings before tickets, each by id descending
        self.assertEqual([t for t, _ in first], ['venue', 'venue', 'event', 'event'])
        venue_ids = sorted((r.id for r in rows if isinstance(r, Booking)), reverse=True)
        self.assertEqual([i for t, i in first if t == 'venue'], venue_ids)
        for _ in range(3):   # a refresh never reshuffles them
            self.assertEqual(self.ids(self.history()), first)

    def test_memberships_join_the_one_list(self):
        venue = self.venue_booking(at(1))
        member = self.membership(at(3))
        event = self.ticket(at(2))
        data = self.history()
        self.assertEqual(self.ids(data), [('venue', member.id), ('event', event.id), ('venue', venue.id)])
        self.assertEqual([i['card'] for i in data['items']], ['membership', 'ticket', 'booking'])

    def test_another_customer_never_sees_my_bookings(self):
        mine = self.venue_booking(at(1))
        self.ticket(at(2), user=self.other)
        self.venue_booking(at(3), user=self.other)
        self.assertEqual(self.ids(self.history()), [('venue', mine.id)])
        theirs = self.history(jwt_client(self.other))
        self.assertEqual(theirs['total'], 2)
        self.assertNotIn(mine.id, [i['id'] for i in theirs['items']])

    def test_signed_out_is_refused(self):
        self.assertEqual(APIClient().get(URL).status_code, 401)

    def test_records_match_the_per_product_endpoints(self):
        booking = self.venue_booking(at(1))
        order = self.ticket(at(2))
        items = {i['id']: i for i in self.history()['items']}
        own_booking = self.client.get('/api/users/me/bookings').data['bookings'][0]
        own_order = self.client.get('/api/users/me/event-orders').data['orders'][0]
        self.assertEqual(items[booking.id]['record'], own_booking)
        self.assertEqual(items[order.id]['record'], own_order)
        self.assertEqual(items[booking.id]['createdAt'], own_booking['createdAt'])

    def test_each_product_keeps_its_visibility_rule(self):
        self.venue_booking(at(1), status='payment_pending')
        self.venue_booking(at(2), status='failed')
        self.venue_booking(at(3), status='removed')
        kept_refund = self.venue_booking(at(4), status='refunded')
        failed_ticket = self.ticket(at(5), status='failed')
        self.ticket(at(6), status='removed')
        self.assertEqual(self.ids(self.history()), [('event', failed_ticket.id), ('venue', kept_refund.id)])

    def test_pages_never_repeat_or_skip_a_row(self):
        made = []
        for hour in range(7):
            made.append(('venue', self.venue_booking(at(hour)).id))
            made.append(('event', self.ticket(at(hour) + datetime.timedelta(minutes=30)).id))
        seen, page = [], 1
        while True:
            data = self.history(page=page, limit=4)
            seen.extend(self.ids(data))
            if not data['hasMore']:
                break
            page += 1
        self.assertEqual(seen, list(reversed(made)))
        self.assertEqual(data['total'], 14)

    def test_bad_paging_is_refused(self):
        for params in ({'limit': 0}, {'limit': 51}, {'page': 0}, {'page': 'x'}):
            self.assertEqual(self.client.get(URL, params).status_code, 400, params)
