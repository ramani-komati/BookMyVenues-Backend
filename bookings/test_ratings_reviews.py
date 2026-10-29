"""
Post-booking ratings & reviews (bookings/ratings.py).

Covers who may rate (owner only), when (only after the slot / show has really
ended, on the SERVER's clock), which bookings (confirmed + verified payment —
never unpaid, cancelled, failed or refunded), the 1–5 range and optional
review text, one rating per booking, the aggregate the cards show, moderation
hiding, the public review lists, and that existing venues and their ids are
untouched by any of it.

The pre-existing venue rating tests live in bookings/tests.py (RatingTests).
"""
import datetime
import uuid
from unittest import mock

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from bookings.models import Booking, Rating
from bookings.ratings import (
    ELIGIBLE,
    NOT_COMPLETED,
    NOT_ELIGIBLE,
    RATED,
    booking_rating_status,
    order_rating_status,
    order_show_ended,
)
from bookings.slots import IST, today_ist
from bookings.tests import BookingTestBase
from venues.models import Listing

YESTERDAY = today_ist() - datetime.timedelta(days=1)


class RatingEligibilityTests(BookingTestBase):
    """Only a finished, verified-paid booking of your own can be rated."""

    def make(self, **overrides):
        fields = {
            'listing': self.listing, 'user': self.customer, 'date': YESTERDAY,
            'slots': ['10:00 – 11:00'], 'amount': 620, 'venue_name': 'Grand Palace Hall',
            'status': 'confirmed', 'razorpay_payment_id': f'pay_{uuid.uuid4().hex[:8]}',
        }
        fields.update(overrides)
        return Booking.objects.create(**fields)

    def rate(self, booking, stars=4, **extra):
        return self.client.post(
            f'/api/venues/{self.listing.id}/ratings',
            {'bookingId': booking.id, 'stars': stars, **extra}, format='json',
        )

    def test_paid_completed_booking_can_be_rated_with_a_review(self):
        booking = self.make()
        response = self.rate(booking, stars=5, review='  Spotless hall, great staff.  ')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, {'rating': 5.0, 'count': 1})
        rating = Rating.objects.get(booking=booking)
        self.assertEqual(rating.review, 'Spotless hall, great staff.')   # trimmed
        self.assertEqual(rating.user, self.customer)

    def test_unpaid_confirmed_booking_cannot_be_rated(self):
        # No Razorpay payment id: the gateway never confirmed money (e.g. a
        # retired pay-at-venue booking).
        booking = self.make(razorpay_payment_id='', method='venue')
        response = self.rate(booking)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'NOT_ELIGIBLE')
        self.assertFalse(Rating.objects.exists())

    def test_cancelled_failed_and_refunded_bookings_cannot_be_rated(self):
        for state in ('cancelled', 'failed', 'refunded', 'refund_pending', 'payment_pending'):
            with self.subTest(state=state):
                booking = self.make(status=state)
                response = self.rate(booking)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['code'], 'NOT_ELIGIBLE')
        self.assertEqual(
            self.rate(self.make(status='cancelled')).data['message'],
            'Cancelled bookings can’t be rated.',
        )
        self.assertEqual(
            self.rate(self.make(status='refunded')).data['message'],
            'Refunded bookings can’t be rated.',
        )
        self.assertFalse(Rating.objects.exists())

    def test_walk_in_cannot_be_rated(self):
        booking = self.make(walk_in=True, method='walk-in')
        self.assertEqual(self.rate(booking).status_code, 409)

    def test_only_the_owner_may_rate(self):
        booking = self.make()
        stranger = User.objects.create_user(phone='9000000077', name='S', email='s7@example.com')
        self.client.force_authenticate(user=stranger)
        self.assertEqual(self.rate(booking).status_code, 403)
        self.assertFalse(Rating.objects.exists())

    def test_a_vendor_cannot_rate_their_own_venue(self):
        booking = self.make()
        self.client.force_authenticate(user=self.vendor)
        self.assertEqual(self.rate(booking).status_code, 403)

    @mock.patch('bookings.ratings.now_minutes_ist', return_value=10 * 60 + 30)
    def test_today_booking_is_not_ratable_until_its_last_slot_ends(self, _now):
        booking = self.make(date=today_ist(), slots=['10:00 – 11:00'])
        response = self.rate(booking)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['message'], 'Booking not completed yet')
        self.assertEqual(booking_rating_status(booking), NOT_COMPLETED)

    @mock.patch('bookings.ratings.now_minutes_ist', return_value=11 * 60)
    def test_today_booking_is_ratable_the_minute_its_slot_ends(self, _now):
        booking = self.make(date=today_ist(), slots=['10:00 – 11:00'])
        self.assertEqual(booking_rating_status(booking), ELIGIBLE)
        self.assertEqual(self.rate(booking).status_code, 201)

    def test_future_booking_cannot_be_rated(self):
        booking = self.make(date=today_ist() + datetime.timedelta(days=3))
        self.assertEqual(self.rate(booking).status_code, 409)

    def test_stars_must_be_a_whole_number_from_1_to_5(self):
        booking = self.make()
        for stars in (0, 6, -1, 4.5, '4.5', 'five', None, ''):
            with self.subTest(stars=stars):
                self.assertEqual(self.rate(booking, stars=stars).status_code, 400)
        self.assertFalse(Rating.objects.exists())
        for stars in (1, 5):
            self.assertEqual(self.rate(self.make(), stars=stars).status_code, 201)

    def test_review_is_optional_but_bounded(self):
        booking = self.make()
        too_long = 'x' * (Rating.MAX_REVIEW_LENGTH + 1)
        self.assertEqual(self.rate(booking, review=too_long).status_code, 400)
        self.assertEqual(self.rate(booking, review=['not', 'text']).status_code, 400)
        self.assertEqual(self.rate(booking).status_code, 201)       # no review at all
        self.assertEqual(Rating.objects.get(booking=booking).review, '')

    def test_duplicate_rating_is_refused_and_the_average_is_untouched(self):
        booking = self.make()
        self.assertEqual(self.rate(booking, stars=2).status_code, 201)
        again = self.rate(booking, stars=5, review='changed my mind')
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data['message'], 'Already rated')
        self.assertEqual(Rating.objects.filter(booking=booking).count(), 1)
        self.assertEqual(Rating.objects.get(booking=booking).stars, 2)

    def test_the_database_allows_one_rating_per_booking(self):
        booking = self.make()
        Rating.objects.create(booking=booking, listing=self.listing, user=self.customer, stars=4)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Rating.objects.create(booking=booking, listing=self.listing, user=self.customer, stars=1)

    def test_a_rating_must_be_for_a_booking_or_a_ticket(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Rating.objects.create(listing=self.listing, user=self.customer, stars=4)

    def test_booking_history_carries_the_servers_rating_status(self):
        done = self.make()
        upcoming = self.make(date=today_ist() + datetime.timedelta(days=2))
        cancelled = self.make(status='cancelled')
        unpaid = self.make(razorpay_payment_id='')

        def statuses():
            rows = self.client.get('/api/users/me/bookings').data['bookings']
            return {row['id']: (row['ratingStatus'], row['userRating'], row['userReview']) for row in rows}

        before = statuses()
        self.assertEqual(before[done.id], (ELIGIBLE, None, None))
        self.assertEqual(before[upcoming.id][0], NOT_COMPLETED)
        self.assertEqual(before[cancelled.id][0], NOT_ELIGIBLE)
        self.assertEqual(before[unpaid.id][0], NOT_ELIGIBLE)

        self.rate(done, stars=4, review='Lovely evening')
        self.assertEqual(statuses()[done.id], (RATED, 4, 'Lovely evening'))

    def test_average_and_count_update_with_each_review(self):
        self.rate(self.make(), stars=5)
        self.rate(self.make(), stars=2)
        response = self.rate(self.make(), stars=4)
        self.assertEqual(response.data, {'rating': round(11 / 3, 4), 'count': 3})

    def test_a_new_rating_is_visible_on_the_venue_page_without_waiting_for_the_cache(self):
        anonymous = APIClient()
        first = anonymous.get(f'/api/venues/{self.listing.id}').data
        self.assertNotIn('rating', first)                     # unrated: no fabricated stars
        self.rate(self.make(), stars=3)
        detail = anonymous.get(f'/api/venues/{self.listing.id}').data   # no cache.clear()
        self.assertEqual((detail['rating'], detail['ratingCount']), (3.0, 1))

    def test_hidden_ratings_leave_the_average_and_the_review_list(self):
        self.rate(self.make(), stars=5, review='Great')
        self.rate(self.make(), stars=1, review='Spam spam spam')
        spam = Rating.objects.get(stars=1)
        from django.utils import timezone
        spam.hidden_at = timezone.now()
        spam.save()
        from bookings.ratings import venue_rating
        self.assertEqual(venue_rating(self.listing), (5.0, 1))
        reviews = APIClient().get(f'/api/venues/{self.listing.id}/reviews').data
        self.assertEqual([r['review'] for r in reviews['reviews']], ['Great'])
        self.assertEqual((reviews['rating'], reviews['count']), (5.0, 1))

    def test_public_reviews_list_written_reviews_with_display_safe_names(self):
        self.customer.name = 'Asha Rani'
        self.customer.save()
        self.rate(self.make(), stars=4)                              # stars only
        self.rate(self.make(), stars=5, review='Will book again')
        data = APIClient().get(f'/api/venues/{self.listing.id}/reviews').data
        self.assertEqual(data['total'], 1)                           # written only
        self.assertEqual(data['count'], 2)                           # both count in the average
        row = data['reviews'][0]
        self.assertEqual(row['customerName'], 'Asha R.')
        flat = str(data)
        self.assertNotIn(self.customer.phone, flat)
        self.assertNotIn(self.customer.email, flat)

    def test_reviews_of_a_hidden_venue_are_not_public(self):
        self.rate(self.make(), stars=5, review='Great')
        Listing.objects.filter(pk=self.listing.pk).update(status='paused')
        self.assertEqual(APIClient().get(f'/api/venues/{self.listing.id}/reviews').status_code, 404)

    def test_rating_never_changes_existing_venue_or_booking_records(self):
        venue_ids = sorted(str(pk) for pk in Listing.objects.values_list('pk', flat=True))
        record = dict(Listing.objects.get(pk=self.listing.pk).record)
        booking = self.make()
        booking_id = booking.id
        self.rate(booking, stars=5, review='Nice')
        self.assertEqual(
            sorted(str(pk) for pk in Listing.objects.values_list('pk', flat=True)), venue_ids
        )
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).record, record)
        self.assertTrue(Booking.objects.filter(pk=booking_id).exists())
        self.assertEqual(Booking.objects.get(pk=booking_id).status, 'confirmed')


class EventRatingTests(TestCase):
    """Event tickets: rated only after the show, only when verified-paid."""

    def setUp(self):
        from events.tests import make_customer, make_event

        cache.clear()
        self.customer = make_customer()
        self.event = make_event(name='Rating Dandiya Night', days_ahead=(-1, 1))
        self.past_date = self.event.dates.order_by('date').first()
        self.client = APIClient()
        self.client.force_authenticate(user=self.customer)

    def order(self, **overrides):
        from events.models import EventOrder

        fields = {
            'event': self.event, 'event_date': self.past_date, 'user': self.customer,
            'event_name': self.event.name, 'date': self.past_date.date,
            'start_time': self.event.start_time, 'end_time': self.event.end_time,
            'items': [{'key': 'adult', 'qty': 2}], 'seats': 2, 'ticket_count': 2,
            'subtotal': 1000, 'fee': 20, 'amount': 1020, 'status': 'confirmed',
            'razorpay_payment_id': f'pay_{uuid.uuid4().hex[:8]}',
        }
        fields.update(overrides)
        return EventOrder.objects.create(**fields)

    def rate(self, order, stars=5, event=None, **extra):
        return self.client.post(
            f'/api/events/{(event or self.event).pk}/ratings',
            {'orderId': order.pk, 'stars': stars, **extra}, format='json',
        )

    def test_attended_paid_ticket_can_be_rated_and_shows_on_event_cards(self):
        response = self.rate(self.order(), stars=4, review='Brilliant music')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data, {'rating': 4.0, 'count': 1})
        cache.clear()   # the public list is cache_page'd for 60 s
        public = APIClient()
        card = next(e for e in public.get('/api/events').data['events'] if e['id'] == str(self.event.pk))
        self.assertEqual((card['rating'], card['ratingCount']), (4.0, 1))
        detail = public.get(f'/api/events/{self.event.slug}').data
        self.assertEqual((detail['rating'], detail['ratingCount']), (4.0, 1))
        reviews = public.get(f'/api/events/{self.event.pk}/reviews').data
        self.assertEqual(reviews['reviews'][0]['review'], 'Brilliant music')

    def test_unrated_event_shows_no_rating(self):
        card = APIClient().get('/api/events').data['events'][0]
        self.assertEqual((card['rating'], card['ratingCount']), (None, 0))

    def test_ticket_for_an_upcoming_show_cannot_be_rated(self):
        upcoming = self.event.dates.order_by('date').last()
        order = self.order(event_date=upcoming, date=upcoming.date)
        response = self.rate(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['message'], 'Event not completed yet')
        self.assertEqual(order_rating_status(order), NOT_COMPLETED)

    def test_unpaid_failed_cancelled_and_refunded_tickets_cannot_be_rated(self):
        for state, payment in (('confirmed', ''), ('failed', 'pay_x'), ('cancelled', 'pay_y'),
                               ('refunded', 'pay_z'), ('refund_pending', 'pay_w')):
            with self.subTest(state=state, payment=payment):
                order = self.order(status=state, razorpay_payment_id=payment)
                self.assertEqual(self.rate(order).status_code, 409)
                self.assertEqual(order_rating_status(order), NOT_ELIGIBLE)
        self.assertFalse(Rating.objects.exists())

    def test_only_the_ticket_holder_may_rate_and_only_once(self):
        from events.tests import make_customer

        order = self.order()
        self.client.force_authenticate(user=make_customer(phone='9100000099', name='Other'))
        self.assertEqual(self.rate(order).status_code, 403)
        self.client.force_authenticate(user=self.customer)
        self.assertEqual(self.rate(order).status_code, 201)
        self.assertEqual(self.rate(order, stars=1).status_code, 409)
        self.assertEqual(Rating.objects.filter(event_order=order).count(), 1)

    def test_a_ticket_cannot_rate_a_different_event(self):
        from events.tests import make_event

        other = make_event(name='Another Night')
        self.assertEqual(self.rate(self.order(), event=other).status_code, 400)

    def test_ticket_history_carries_rating_status(self):
        order = self.order()
        rows = self.client.get('/api/users/me/event-orders').data['orders']
        self.assertEqual(rows[0]['ratingStatus'], ELIGIBLE)
        self.rate(order, stars=3)
        rows = self.client.get('/api/users/me/event-orders').data['orders']
        self.assertEqual((rows[0]['ratingStatus'], rows[0]['userRating']), (RATED, 3))

    def test_show_end_time_including_overnight_shows(self):
        from events.models import EventOrder

        day = datetime.date(2026, 10, 10)
        evening = EventOrder(date=day, start_time=datetime.time(18, 0), end_time=datetime.time(22, 0))
        at = lambda d, h, m=0: datetime.datetime.combine(d, datetime.time(h, m), tzinfo=IST)  # noqa: E731
        self.assertFalse(order_show_ended(evening, now=at(day, 21, 59)))
        self.assertTrue(order_show_ended(evening, now=at(day, 22, 0)))

        overnight = EventOrder(date=day, start_time=datetime.time(21, 0), end_time=datetime.time(2, 0))
        next_day = day + datetime.timedelta(days=1)
        self.assertFalse(order_show_ended(overnight, now=at(day, 23, 30)))
        self.assertFalse(order_show_ended(overnight, now=at(next_day, 1, 59)))
        self.assertTrue(order_show_ended(overnight, now=at(next_day, 2, 0)))

        to_midnight = EventOrder(date=day, start_time=datetime.time(19, 0), end_time=datetime.time(0, 0))
        self.assertFalse(order_show_ended(to_midnight, now=at(day, 23, 59)))
        self.assertTrue(order_show_ended(to_midnight, now=at(next_day, 0, 0)))
