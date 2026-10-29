"""
Vendor insights and customer-experience controls:

  * dashboard trends are omitted (None) when there is nothing to compare
  * the analytics block separates booking value, captured payments, refunds
    and net earnings, from stored rows only
  * occupancy uses the venue's real opening hours and never invents a 0 %
  * vendors reply to reviews of their OWN venues/events; they can never touch
    the customer's stars or words; the super admin can hide a reply
  * event sales status and per-ticket-type sales for the vendor's events
"""
import datetime
import uuid

from django.utils import timezone

from accounts.models import User
from events.models import EventOrder
from events.tests import admin_client, make_admin, make_event
from venues.models import Listing, VenueBusinessSettings

from .models import Booking, Rating
from .slots import today_ist
from .tests import BookingTestBase
from .vendor_views import _trend

YESTERDAY = today_ist() - datetime.timedelta(days=1)


class TrendTests(BookingTestBase):
    def test_no_trend_without_a_previous_period(self):
        self.assertIsNone(_trend(500, 0))
        self.assertIsNone(_trend(0, 0))

    def test_real_change_is_reported(self):
        self.assertEqual(_trend(150, 100), 50)
        self.assertEqual(_trend(60, 100), -40)

    def test_dashboard_sends_none_instead_of_a_fake_hundred_percent(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(), slots=['22:00 – 23:00'],
            amount=620, fee=20, status='confirmed', razorpay_payment_id='pay_1',
            venue_name='Grand Palace Hall',
        )
        self.client.force_authenticate(user=self.vendor)
        stats = self.client.get('/api/vendors/me/dashboard').data['stats']
        self.assertEqual(stats['today']['value'], 600)
        self.assertIsNone(stats['today']['trend'])


class AnalyticsTests(BookingTestBase):
    def make(self, **fields):
        base = {
            'listing': self.listing, 'user': self.customer, 'date': YESTERDAY,
            'slots': ['18:00 – 20:00'], 'amount': 1220, 'fee': 20, 'status': 'confirmed',
            'razorpay_payment_id': f'pay_{uuid.uuid4().hex[:8]}', 'venue_name': 'Grand Palace Hall',
        }
        base.update(fields)
        return Booking.objects.create(**base)

    def analytics(self):
        self.client.force_authenticate(user=self.vendor)
        return self.client.get('/api/vendors/me/dashboard').data['analytics']

    def test_money_terms_are_kept_apart(self):
        self.make()                                                    # paid, kept
        self.make(amount=600, fee=0, walk_in=True, method='walk-in',
                  razorpay_payment_id='', slots=['10:00 – 11:00'])     # cash walk-in
        self.make(amount=920, status='refunded', refund_amount=920)    # paid then refunded
        self.make(amount=920, status='cancelled', razorpay_payment_id='')  # abandoned
        money = self.analytics()['money']['month']
        self.assertEqual(money['bookingValue'], 1220 + 600)
        self.assertEqual(money['captured'], 1220 + 920)
        self.assertEqual(money['refunded'], 920)
        self.assertEqual(money['net'], 1200 + 600)

    def test_counts_statuses_and_channels(self):
        self.make(date=today_ist())
        self.make()
        self.make(date=today_ist() + datetime.timedelta(days=2))
        self.make(status='cancelled')
        self.make(status='refund_pending')
        self.make(amount=600, fee=0, walk_in=True, method='walk-in', razorpay_payment_id='')
        data = self.analytics()
        self.assertEqual(data['bookings']['today'], 1)
        self.assertEqual(data['bookings']['week'], 3)
        self.assertEqual(data['bookings']['upcoming'], 1)
        self.assertEqual(data['statuses']['cancelled'], 1)
        self.assertEqual(data['statuses']['refundPending'], 1)
        self.assertEqual(data['channels'], {'platform': 2, 'walkIn': 1})

    def test_top_venues_and_popular_hours_come_from_bookings(self):
        self.make(slots=['18:00 – 20:00'])
        self.make(slots=['19:00 – 20:00'], venue_name='Grand Palace Hall — Pitch 2')
        data = self.analytics()
        self.assertEqual(data['topVenues'], [{'venue': 'Grand Palace Hall', 'bookings': 2}])
        self.assertEqual(data['popularHours'][0], {'start': '19:00', 'end': '20:00', 'bookings': 2})

    def test_occupancy_uses_opening_hours(self):
        Listing.objects.filter(pk=self.listing.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=10),
            status='live',
        )
        self.make()                           # 2 hours yesterday
        row = self.analytics()['occupancy']['venues'][0]
        self.assertEqual(row['bookedHours'], 2.0)
        self.assertGreater(row['openHours'], 18 * 9)       # 9 full days at least
        self.assertEqual(row['percent'], round(2 / row['openHours'] * 100))

    def test_no_open_hours_means_no_occupancy_figure(self):
        Listing.objects.filter(pk=self.listing.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=10), status='live',
        )
        VenueBusinessSettings.objects.create(listing=self.listing, hours={str(i): [] for i in range(7)})
        row = self.analytics()['occupancy']['venues'][0]
        self.assertIsNone(row['percent'])

    def test_ratings_summary(self):
        for stars in (5, 4):
            booking = self.make()
            Rating.objects.create(booking=booking, listing=self.listing, user=self.customer, stars=stars)
        self.assertEqual(self.analytics()['ratings']['average'], 4.5)
        self.assertEqual(self.analytics()['ratings']['count'], 2)


class ReviewReplyTests(BookingTestBase):
    def setUp(self):
        super().setUp()
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=YESTERDAY, slots=['10:00 – 11:00'],
            amount=620, status='confirmed', razorpay_payment_id='pay_r', venue_name='Grand Palace Hall',
        )
        Listing.objects.filter(pk=self.listing.pk).update(status='live')
        self.rating = Rating.objects.create(
            booking=booking, listing=self.listing, user=self.customer,
            stars=2, review='Lights were not working.',
        )
        self.url = f'/api/vendors/me/ratings/{self.rating.pk}/reply'

    def reply(self, text='Sorry — fixed now.', user=None):
        self.client.force_authenticate(user=user or self.vendor)
        return self.client.put(self.url, {'reply': text}, format='json')

    def public(self):
        self.client.force_authenticate(user=None)
        return self.client.get(f'/api/venues/{self.listing.id}/reviews').data['reviews'][0]

    def test_vendor_reply_is_public_and_the_review_is_untouched(self):
        response = self.reply()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['rating']['reply'], 'Sorry — fixed now.')
        review = self.public()
        self.assertEqual(review['reply']['text'], 'Sorry — fixed now.')
        self.assertEqual((review['stars'], review['review']), (2, 'Lights were not working.'))
        self.rating.refresh_from_db()
        self.assertEqual((self.rating.stars, self.rating.review), (2, 'Lights were not working.'))

    def test_reply_cannot_change_stars_or_review(self):
        self.client.force_authenticate(user=self.vendor)
        self.client.put(self.url, {'reply': 'Thanks', 'stars': 5, 'review': 'Great!'}, format='json')
        self.rating.refresh_from_db()
        self.assertEqual((self.rating.stars, self.rating.review), (2, 'Lights were not working.'))

    def test_validation_and_ownership(self):
        self.assertEqual(self.reply('   ').status_code, 400)
        self.assertEqual(self.reply('x' * (Rating.MAX_REPLY_LENGTH + 1)).status_code, 400)
        other = User.objects.create_user(phone='9000000077', name='O', email='o7@example.com',
                                         role=User.Role.VENDOR)
        self.assertEqual(self.reply(user=other).status_code, 404)
        self.client.force_authenticate(user=self.customer)
        self.assertEqual(self.client.put(self.url, {'reply': 'x'}, format='json').status_code, 403)

    def test_no_reply_to_a_review_moderation_hid(self):
        self.rating.hidden_at = timezone.now()
        self.rating.save()
        self.assertEqual(self.reply().status_code, 404)

    def test_admin_can_hide_a_reply_and_the_vendor_cannot_rewrite_it(self):
        self.reply()
        admin = admin_client(make_admin())
        response = admin.patch(f'/api/admin/ratings/{self.rating.pk}',
                               {'replyHidden': True, 'reason': 'Shares a phone number'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['replyHidden'])
        self.assertIsNone(self.public()['reply'])
        self.assertEqual(self.reply('New text').status_code, 409)
        self.client.force_authenticate(user=self.vendor)
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.rating.refresh_from_db()
        self.assertEqual(self.rating.reply, '')

    def test_hiding_a_reply_needs_a_reason(self):
        self.reply()
        admin = admin_client(make_admin())
        response = admin.patch(f'/api/admin/ratings/{self.rating.pk}', {'replyHidden': True}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_vendor_list_filters_and_shows_replies(self):
        self.reply()
        self.client.force_authenticate(user=self.vendor)
        data = self.client.get('/api/vendors/me/ratings?stars=2').data
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['ratings'][0]['reply'], 'Sorry — fixed now.')
        self.assertEqual(self.client.get('/api/vendors/me/ratings?stars=5').data['total'], 0)
        self.assertEqual(self.client.get('/api/vendors/me/ratings?kind=event').data['total'], 0)
        self.assertEqual(self.client.get('/api/vendors/me/ratings?stars=9').status_code, 400)

    def test_event_ratings_reach_the_events_vendor(self):
        event = make_event(self.vendor, name='Vendor Night')
        date = event.dates.first()
        order = EventOrder.objects.create(event=event, event_date=date, date=date.date,
                                          user=self.customer, status='confirmed', seats=1,
                                          ticket_count=1, amount=520, razorpay_payment_id='pay_e')
        Rating.objects.create(event_order=order, event=event, user=self.customer, stars=5, review='Loved it')
        self.client.force_authenticate(user=self.vendor)
        data = self.client.get('/api/vendors/me/ratings?kind=event').data
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['ratings'][0]['eventName'], 'Vendor Night')
        self.assertEqual(self.client.get('/api/vendors/me/ratings').data['total'], 2)


class EventSalesStatusTests(BookingTestBase):
    def records(self):
        self.client.force_authenticate(user=self.vendor)
        return {e['name']: e for e in self.client.get('/api/vendors/me/events').data['events']}

    def test_sales_status_and_sold_per_ticket_type(self):
        on_sale = make_event(self.vendor, name='On Sale', capacity=10)
        date = on_sale.dates.first()
        EventOrder.objects.create(
            event=on_sale, event_date=date, date=date.date, status='confirmed', seats=3,
            ticket_count=3, amount=1270, razorpay_payment_id='pay_s',
            items=[{'key': 'adult', 'label': 'Adult', 'qty': 2, 'price': 500},
                   {'key': 'child', 'label': 'Child', 'qty': 1, 'price': 250}],
        )
        sold_out = make_event(self.vendor, name='Sold Out', capacity=1)
        date = sold_out.dates.first()
        EventOrder.objects.create(event=sold_out, event_date=date, date=date.date, status='confirmed',
                                  seats=1, ticket_count=1, amount=500, razorpay_payment_id='pay_t')
        make_event(self.vendor, name='Waiting', status='pending')

        records = self.records()
        self.assertEqual(records['On Sale']['salesStatus'], 'on_sale')
        self.assertEqual(records['Sold Out']['salesStatus'], 'sold_out')
        self.assertEqual(records['Waiting']['salesStatus'], 'not_live')
        sold = {t['key']: t['sold'] for t in records['On Sale']['ticketTypes']}
        self.assertEqual(sold, {'adult': 2, 'child': 1})
