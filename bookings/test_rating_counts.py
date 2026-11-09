"""
The rating customers see — "4.5 ★ (10)" or "Not rated yet" — is one number,
computed one way (bookings/ratings.counted_ratings), everywhere: venue cards,
the venue page, the review list, the homepage and the admin panel.

Only ratings from bookings that are STILL confirmed and verified-paid count;
a booking refunded or cancelled after it was rated drops out, hidden ratings
drop out, and nothing stored in a venue's record can stand in for the live
aggregate.
"""
import datetime
import uuid

from django.core.cache import cache
from rest_framework.test import APIClient

from bookings.models import Booking, Rating
from bookings.ratings import counted_ratings, rating_payload, venue_rating
from bookings.slots import today_ist
from bookings.tests import BookingTestBase
from venues.models import Listing

YESTERDAY = today_ist() - datetime.timedelta(days=1)


class RatingCountTests(BookingTestBase):

    def setUp(self):
        super().setUp()
        cache.clear()
        self.public = APIClient()

    def make(self, listing=None, **overrides):
        fields = {
            'listing': listing or self.listing, 'user': self.customer, 'date': YESTERDAY,
            'slots': ['10:00 – 11:00'], 'amount': 620, 'venue_name': 'Grand Palace Hall',
            'status': 'confirmed', 'razorpay_payment_id': f'pay_{uuid.uuid4().hex[:8]}',
        }
        fields.update(overrides)
        return Booking.objects.create(**fields)

    def rate(self, booking, stars, listing=None):
        listing = listing or self.listing
        return self.client.post(
            f'/api/venues/{listing.id}/ratings',
            {'bookingId': booking.id, 'stars': stars}, format='json',
        )

    def card(self, listing=None):
        cache.clear()   # the list is cache_page'd for 60 s; read it fresh
        rows = self.public.get('/api/venues?limit=50').data['venues']
        return next(v for v in rows if v['id'] == str((listing or self.listing).id))

    def detail(self, listing=None):
        return self.public.get(f'/api/venues/{(listing or self.listing).id}').data

    def test_average_and_count_are_exact_and_the_same_on_card_page_and_reviews(self):
        for stars in (5, 4, 4):
            self.assertEqual(self.rate(self.make(), stars).status_code, 201)
        card, page = self.card(), self.detail()
        reviews = self.public.get(f'/api/venues/{self.listing.id}/reviews').data
        self.assertAlmostEqual(card['rating'], 13 / 3, places=4)
        self.assertEqual(card['ratingCount'], 3)
        self.assertEqual((page['rating'], page['ratingCount']), (card['rating'], card['ratingCount']))
        self.assertEqual((reviews['rating'], reviews['count']), (card['rating'], card['ratingCount']))

    def test_no_valid_ratings_reads_as_null_and_zero_everywhere(self):
        card, page = self.card(), self.detail()
        self.assertEqual((card['rating'], card['ratingCount']), (None, 0))
        self.assertEqual((page['rating'], page['ratingCount']), (None, 0))

    def test_a_booking_refunded_or_cancelled_after_rating_stops_counting(self):
        keep = self.make()
        refunded = self.make()
        cancelled = self.make()
        self.rate(keep, 5)
        self.rate(refunded, 1)
        self.rate(cancelled, 1)
        self.assertEqual(venue_rating(self.listing), (round(7 / 3, 4), 3))
        Booking.objects.filter(pk=refunded.pk).update(status='refunded')
        Booking.objects.filter(pk=cancelled.pk).update(status='cancelled')
        self.assertEqual(venue_rating(self.listing), (5.0, 1))
        self.assertEqual((self.card()['rating'], self.card()['ratingCount']), (5.0, 1))
        reviews = self.public.get(f'/api/venues/{self.listing.id}/reviews').data
        self.assertEqual(reviews['count'], 1)
        # The stored ratings themselves are untouched.
        self.assertEqual(Rating.objects.count(), 3)

    def test_an_unpaid_booking_rating_never_counts(self):
        """Even a rating row that slipped in for an unpaid booking (legacy
        data) is excluded from the aggregate."""
        unpaid = self.make(razorpay_payment_id='')
        Rating.objects.create(booking=unpaid, listing=self.listing, user=self.customer, stars=1)
        self.assertEqual(venue_rating(self.listing), (None, 0))
        self.assertFalse(counted_ratings().exists())

    def test_one_rating_per_booking_so_a_double_submit_is_not_counted_twice(self):
        booking = self.make()
        self.assertEqual(self.rate(booking, 5).status_code, 201)
        self.assertEqual(self.rate(booking, 1).status_code, 409)
        self.assertEqual(venue_rating(self.listing), (5.0, 1))

    def test_a_rating_stored_in_the_venue_record_is_never_shown(self):
        record = dict(self.listing.record)
        record.update({'rating': 4.9, 'ratingCount': 120})   # e.g. imported mock data
        Listing.objects.filter(pk=self.listing.pk).update(record=record)
        cache.clear()
        card = self.card()
        self.assertEqual((card['rating'], card['ratingCount']), (None, 0))
        self.assertEqual((self.detail()['rating'], self.detail()['ratingCount']), (None, 0))

    def test_a_new_rating_updates_the_average_and_count(self):
        self.rate(self.make(), 5)
        self.assertEqual((self.card()['rating'], self.card()['ratingCount']), (5.0, 1))
        response = self.rate(self.make(), 4)
        self.assertEqual(response.data, {'rating': 4.5, 'count': 2})
        self.assertEqual((self.card()['rating'], self.card()['ratingCount']), (4.5, 2))

    def test_admin_panel_shows_the_same_average_as_customers(self):
        from events.tests import admin_client, make_admin

        self.rate(self.make(), 5)
        refunded = self.make()
        self.rate(refunded, 1)
        Booking.objects.filter(pk=refunded.pk).update(status='refunded')
        data = admin_client(make_admin()).get('/api/admin/bootstrap').data
        row = next(v for v in data['venues'] if v['id'] == str(self.listing.id))
        self.assertEqual(row['rating'], 5.0)

    def test_rating_payload_shape(self):
        self.assertEqual(rating_payload(None, 0), {'rating': None, 'ratingCount': 0})
        self.assertEqual(rating_payload(4.5, 0), {'rating': None, 'ratingCount': 0})
        self.assertEqual(rating_payload(4.5, 10), {'rating': 4.5, 'ratingCount': 10})
