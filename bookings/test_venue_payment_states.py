"""
Venue booking payment states — holds, failures, retries and removal.

The rules under test:
  * an unpaid hold blocks its slot for exactly PENDING_HOLD_MINUTES, and
    Booking.hold_expired flips at the same instant availability frees it
  * payment.failed records 'failed' (slot released) — never 'cancelled'
  * a verified RETRY inside the same checkout confirms a 'failed' booking
    (it used to be auto-refunded), unless the slot was re-sold meanwhile
  * a stale failure never overwrites a confirmed booking
  * cancelled / removed bookings are never revived: a late capture refunds
  * 'failed' and 'removed' rows never block a slot
"""
import datetime
from unittest.mock import MagicMock, patch

from django.utils import timezone

from .models import Booking
from . import tests as base
from .tests import TOMORROW, BookingTestBase
from .views import PENDING_HOLD_MINUTES


class VenuePaymentStateTests(BookingTestBase):
    # The payment-flow helpers, borrowed via the module so the runner does
    # not collect PaymentFlowTests a second time from this file.
    order = base.PaymentFlowTests.order
    _sign = base.PaymentFlowTests._sign
    _webhook = base.PaymentFlowTests._webhook

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def pending(self):
        response = self.order()
        self.assertEqual(response.status_code, 201)
        return Booking.objects.get(pk=response.data['bookingId'])

    def age(self, booking, minutes):
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=minutes)
        )
        booking.refresh_from_db()

    def verify(self, booking, payment_id='pay_RETRY'):
        return self.client.post('/api/payments/verify', {
            'bookingId': booking.id,
            'razorpay_order_id': booking.razorpay_order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': self._sign(booking.razorpay_order_id, payment_id),
        }, format='json')

    def slot_free(self):
        return self.book().status_code == 201

    def refund_mocks(self):
        refund_ok = MagicMock(status_code=200)
        refund_ok.json.return_value = {'id': 'rfnd_AUTO'}
        no_existing = MagicMock(status_code=200)
        no_existing.json.return_value = {'items': []}
        return (patch('bookings.razorpay_client.requests.post', return_value=refund_ok),
                patch('bookings.razorpay_client.requests.get', return_value=no_existing))

    # ---- hold expiry --------------------------------------------------------

    def test_hold_lasts_the_configured_window(self):
        self.assertEqual(PENDING_HOLD_MINUTES, 30)
        booking = self.pending()
        self.age(booking, PENDING_HOLD_MINUTES - 1)
        self.assertFalse(booking.hold_expired)
        self.assertFalse(self.slot_free())        # still held

    def test_hold_expired_flips_when_availability_frees_the_slot(self):
        booking = self.pending()
        self.age(booking, PENDING_HOLD_MINUTES + 1)
        self.assertTrue(booking.hold_expired)
        self.assertTrue(self.slot_free())

    def test_only_unpaid_holds_can_expire(self):
        booking = self.pending()
        self.age(booking, PENDING_HOLD_MINUTES + 60)
        for status in ('confirmed', 'failed', 'cancelled', 'refunded', 'removed'):
            booking.status = status
            self.assertFalse(booking.hold_expired, status)

    # ---- failure and retry --------------------------------------------------

    def test_failed_payment_is_recorded_as_failed_and_frees_the_slot(self):
        booking = self.pending()
        self._webhook('payment.failed', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'failed')
        self.assertEqual(booking.as_record()['status'], 'failed')
        self.assertTrue(self.slot_free())

    def test_retry_after_failure_confirms_via_verify(self):
        booking = self.pending()
        self._webhook('payment.failed', booking.razorpay_order_id)
        response = self.verify(booking)
        self.assertEqual(response.status_code, 200, response.data)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.razorpay_payment_id, 'pay_RETRY')
        self.assertFalse(self.slot_free())        # the slot is taken again

    def test_retry_after_failure_confirms_via_webhook(self):
        booking = self.pending()
        self._webhook('payment.failed', booking.razorpay_order_id)
        self._webhook('payment.captured', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.refund_id, '')   # NOT auto-refunded

    def test_retry_after_the_slot_was_resold_queues_a_refund(self):
        booking = self.pending()
        self._webhook('payment.failed', booking.razorpay_order_id)
        self.assertTrue(self.slot_free())         # someone else books it
        response = self.verify(booking)
        self.assertEqual(response.status_code, 409)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refund_pending')
        self.assertEqual(booking.razorpay_payment_id, 'pay_RETRY')

    def test_a_stale_failure_never_overwrites_a_paid_booking(self):
        booking = self.pending()
        self.assertEqual(self.verify(booking, 'pay_OK').status_code, 200)
        self._webhook('payment.failed', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')

    def test_a_failure_never_overwrites_a_customer_cancel(self):
        booking = self.pending()
        self.client.delete(f'/api/users/me/bookings/{booking.id}')
        self._webhook('payment.failed', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')

    def test_verify_replay_is_idempotent(self):
        booking = self.pending()
        self.assertEqual(self.verify(booking, 'pay_OK').status_code, 200)
        self.assertEqual(self.verify(booking, 'pay_OK').status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')

    # ---- never revived ------------------------------------------------------

    def test_capture_after_cancel_still_refunds(self):
        booking = self.pending()
        self.client.delete(f'/api/users/me/bookings/{booking.id}')
        post, get = self.refund_mocks()
        with post, get:
            self._webhook('payment.captured', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refunded')
        self.assertEqual(self.verify(booking).status_code, 409)

    def test_capture_after_admin_removal_refunds_and_stays_removed(self):
        booking = self.pending()
        Booking.objects.filter(pk=booking.pk).update(status='removed')
        self.assertEqual(self.verify(booking).status_code, 409)
        post, get = self.refund_mocks()
        with post, get:
            self._webhook('payment.captured', booking.razorpay_order_id)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refunded')
        self.assertEqual(booking.refund_id, 'rfnd_AUTO')

    # ---- dead rows ----------------------------------------------------------

    def test_failed_and_removed_rows_never_block_a_slot(self):
        for status in ('failed', 'removed'):
            with self.subTest(status=status):
                Booking.objects.all().delete()
                Booking.objects.create(
                    listing=self.listing, user=self.customer,
                    date=datetime.date.fromisoformat(TOMORROW),
                    slots=['19:30 – 21:00'], amount=920, fee=20, status=status,
                    venue_name='Grand Palace Hall',
                )
                self.assertTrue(self.slot_free())

    def test_dead_and_removable_sets(self):
        self.assertEqual(
            set(Booking.DEAD_STATUSES), {'cancelled', 'refunded', 'failed', 'removed'}
        )
        # Money-bearing states are never removable.
        for status in ('confirmed', 'completed', 'refund_pending', 'refunded'):
            self.assertNotIn(status, Booking.REMOVABLE_STATUSES)
