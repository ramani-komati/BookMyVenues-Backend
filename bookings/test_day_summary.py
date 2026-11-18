"""
The customer date strip's per-day summary (bookings/day_summary.py) and the
shared demand band (bookings/demand.py), plus the coarse band on event
nights.

    GET /api/venues/<id>/availability/days?from=YYYY-MM-DD&days=N

Every number comes from the same rows the booking check reads: confirmed
bookings and live 5-minute holds take time, expired holds and dead rows do
not, vendor closures / blocks remove supply, and the signed-in viewer's own
hold is not shown back to them as taken.
"""
import datetime
from unittest.mock import patch

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from events.models import EventOrder
from events.tests import EventTestCase, make_customer, make_event
from venues.models import SlotBlock, VenueBusinessSettings

from .demand import demand_band
from .models import Booking
from .slots import today_ist
from .tests import BookingTestBase

TOMORROW = today_ist() + datetime.timedelta(days=1)
GRID = 18                       # 06:00 … 23:00 one-hour starts


def hours(first, count):
    return [f'{h:02d}:00 – {h + 1:02d}:00' for h in range(first, first + count)]


class DemandBandTests(SimpleTestCase):
    def test_bands(self):
        self.assertEqual(demand_band(18, 18), 'available')
        self.assertEqual(demand_band(9, 18), 'available')        # exactly half free
        self.assertEqual(demand_band(8, 18), 'filling_fast')
        self.assertEqual(demand_band(4, 20), 'almost_full')      # a fifth free
        self.assertEqual(demand_band(1, 18), 'almost_full')
        self.assertEqual(demand_band(0, 18), 'sold_out')

    def test_no_finite_capacity_has_no_band(self):
        self.assertIsNone(demand_band(None, None))
        self.assertIsNone(demand_band(5, 0))

    def test_out_of_range_counts_are_clamped(self):
        self.assertEqual(demand_band(-3, 10), 'sold_out')
        self.assertEqual(demand_band(30, 10), 'available')


class DaySummaryEndpointTests(BookingTestBase):
    def url(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items())
        return f'/api/venues/{self.listing.id}/availability/days' + (f'?{query}' if query else '')

    def book_row(self, slots, day=TOMORROW, status='confirmed', **extra):
        return Booking.objects.create(
            listing=self.listing, user=self.customer, date=day, slots=slots,
            amount=620, venue_name='Grand Palace Hall', customer_name='Asha',
            status=status, **extra,
        )

    def day(self, response, iso):
        return next(row for row in response.data['days'] if row['date'] == iso)

    def test_free_day_is_available_with_the_full_grid(self):
        response = APIClient().get(self.url(**{'from': TOMORROW.isoformat(), 'days': 3}))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['days']), 3)
        row = self.day(response, TOMORROW.isoformat())
        self.assertEqual((row['status'], row['freeSlots'], row['totalSlots']), ('available', GRID, GRID))
        self.assertFalse(row['closed'])

    def test_bookings_and_live_holds_fill_the_day(self):
        self.book_row(hours(6, 10), razorpay_payment_id='pay_1')              # 10 h booked
        held = self.book_row(hours(16, 2), status='payment_pending')          # 2 h held
        Booking.objects.filter(pk=held.pk).update(
            hold_expires_at=timezone.now() + datetime.timedelta(minutes=4))
        row = self.day(APIClient().get(self.url(**{'from': TOMORROW.isoformat()})), TOMORROW.isoformat())
        self.assertEqual(row['freeSlots'], GRID - 12)
        self.assertEqual(row['status'], 'filling_fast')

    def test_expired_holds_and_dead_rows_free_the_time(self):
        expired = self.book_row(hours(6, 17), status='payment_pending')
        Booking.objects.filter(pk=expired.pk).update(
            hold_expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.book_row(hours(6, 17), status='cancelled')
        self.book_row(hours(6, 17), status='refunded', razorpay_payment_id='pay_r')
        row = self.day(APIClient().get(self.url(**{'from': TOMORROW.isoformat()})), TOMORROW.isoformat())
        self.assertEqual((row['status'], row['freeSlots']), ('available', GRID))

    def test_fully_booked_day_is_sold_out_and_nearly_full_is_almost_full(self):
        self.book_row(hours(6, 18), razorpay_payment_id='pay_full')
        later = TOMORROW + datetime.timedelta(days=1)
        self.book_row(hours(6, 15), day=later, razorpay_payment_id='pay_most')
        response = APIClient().get(self.url(**{'from': TOMORROW.isoformat(), 'days': 2}))
        self.assertEqual(self.day(response, TOMORROW.isoformat())['status'], 'sold_out')
        self.assertEqual(self.day(response, later.isoformat())['status'], 'almost_full')

    def test_closure_and_blocks_come_from_the_vendor_schedule(self):
        later = TOMORROW + datetime.timedelta(days=1)
        VenueBusinessSettings.objects.create(
            listing=self.listing,
            closures=[{'from': TOMORROW.isoformat(), 'to': TOMORROW.isoformat(), 'reason': 'Diwali'}],
        )
        SlotBlock.objects.create(listing=self.listing, date=later, start_minute=600, end_minute=720)
        response = APIClient().get(self.url(**{'from': TOMORROW.isoformat(), 'days': 2}))
        closed = self.day(response, TOMORROW.isoformat())
        self.assertEqual((closed['status'], closed['closed'], closed['reason']), ('closed', True, 'Diwali'))
        blocked = self.day(response, later.isoformat())
        self.assertEqual((blocked['freeSlots'], blocked['totalSlots']), (GRID - 2, GRID - 2))

    def test_the_viewers_own_hold_is_not_shown_as_taken(self):
        mine = self.book_row(hours(6, 18), status='payment_pending')
        Booking.objects.filter(pk=mine.pk).update(
            hold_expires_at=timezone.now() + datetime.timedelta(minutes=4))
        anon = self.day(APIClient().get(self.url(**{'from': TOMORROW.isoformat()})), TOMORROW.isoformat())
        own = self.day(self.client.get(self.url(**{'from': TOMORROW.isoformat()})), TOMORROW.isoformat())
        self.assertEqual(anon['status'], 'sold_out')
        self.assertEqual(own['status'], 'available')

    def test_today_drops_hours_already_started(self):
        with patch('bookings.day_summary.now_minutes_ist', return_value=20 * 60 + 10):
            row = self.day(APIClient().get(self.url()), today_ist().isoformat())
        # 21:00, 22:00, 23:00 remain (20:00 has started).
        self.assertEqual((row['freeSlots'], row['totalSlots']), (3, 3))
        with patch('bookings.day_summary.now_minutes_ist', return_value=23 * 60 + 30):
            row = self.day(APIClient().get(self.url()), today_ist().isoformat())
        # Nothing left to sell, but nothing was sold either.
        self.assertEqual(row['status'], 'unavailable')

    def test_bad_requests(self):
        past = (today_ist() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(APIClient().get(self.url(**{'from': past})).status_code, 400)
        self.assertEqual(APIClient().get(self.url(**{'from': 'soon'})).status_code, 400)
        self.assertEqual(APIClient().get(self.url(days='lots')).status_code, 400)
        far = (today_ist() + datetime.timedelta(days=400)).isoformat()
        self.assertEqual(APIClient().get(self.url(**{'from': far})).status_code, 400)
        self.assertEqual(len(APIClient().get(self.url(days=90)).data['days']), 14)   # capped

    def test_unknown_or_hidden_venue_is_404(self):
        self.listing.status = 'pending'
        self.listing.save(update_fields=['status'])
        self.assertEqual(APIClient().get(self.url()).status_code, 404)


class EventDemandBandTests(EventTestCase):
    def order(self, event_date, seats, status='confirmed'):
        event = event_date.event
        return EventOrder.objects.create(
            event=event, event_date=event_date, user=make_customer(phone=f'91000{seats:05d}'),
            event_name=event.name, date=event_date.date,
            start_time=event.start_time, end_time=event.end_time,
            seats=seats, ticket_count=seats, amount=520, subtotal=500, fee=20, status=status,
        )

    def test_nights_carry_a_band_but_never_a_count(self):
        event = make_event(capacity=10)
        night = event.dates.first()
        url = reverse('public-event-availability', args=[event.id]) + f'?date={night.date.isoformat()}'
        detail_url = reverse('public-event-detail', args=[event.slug])

        self.assertEqual(APIClient().get(url).data['demand'], 'available')
        self.order(night, 6)
        data = APIClient().get(url).data
        self.assertEqual(data['demand'], 'filling_fast')
        self.assertNotIn('remaining', data)
        self.order(night, 2)
        self.assertEqual(APIClient().get(url).data['demand'], 'almost_full')

        row = APIClient().get(detail_url).data['dates'][0]
        self.assertIn(row['demand'], ('almost_full', 'filling_fast', 'available'))
        self.assertNotIn('remaining', row)

    def test_unlimited_night_has_no_band(self):
        event = make_event(capacity=None)
        night = event.dates.first()
        url = reverse('public-event-availability', args=[event.id]) + f'?date={night.date.isoformat()}'
        self.assertIsNone(APIClient().get(url).data['demand'])


class CustomerRefundAmountTests(BookingTestBase):
    """The customer's own record says how much was actually refunded."""

    def test_partial_refund_amount_is_on_the_customer_record(self):
        day = today_ist() + datetime.timedelta(days=3)
        refunded = Booking.objects.create(
            listing=self.listing, user=self.customer, date=day, slots=['10:00 – 11:00'],
            amount=1020, venue_name='Grand Palace Hall', customer_name='Asha',
            status='refunded', razorpay_payment_id='pay_part', refund_amount=520,
        )
        kept = Booking.objects.create(
            listing=self.listing, user=self.customer, date=day, slots=['12:00 – 13:00'],
            amount=620, venue_name='Grand Palace Hall', customer_name='Asha',
            status='confirmed', razorpay_payment_id='pay_kept',
        )
        self.assertEqual(refunded.as_record()['refundAmount'], 520)
        self.assertIsNone(kept.as_record()['refundAmount'])
