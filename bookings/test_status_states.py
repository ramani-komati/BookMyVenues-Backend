"""
Booking status vs payment status (bookings/status_view.py) — regression tests.

  * a successful (verified) payment → booking confirmed, payment completed
  * a confirmed booking whose slot has ended → booking completed, payment
    still completed (one never implies the other)
  * failed payments, abandoned checkouts, cancellations and refunds each
    read as what they are, on BOTH questions
  * a confirmed row with no gateway capture is never "payment completed"
  * the client cannot mark a payment completed: an unsigned verify, or any
    extra field in the body, leaves the booking pending
  * a redelivered capture webhook on a refunded booking does not refund it
    again (it used to flip the row back to refund_pending)
  * every record the apps read (customer history, verify answer, admin
    bootstrap) carries bookingState / paymentState, and event orders follow
    the same rules
"""
import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from . import tests as base
from .models import Booking
from .slots import today_ist
from .status_view import _states, order_states, state_fields
from .tests import BookingTestBase


def states(status, display=None, **facts):
    facts = {'hold_expired': False, 'captured': False, 'offline': False, 'amount': 920, **facts}
    return _states(status, display or status, **facts)


class StateRulesTests(SimpleTestCase):
    def test_successful_payment(self):
        self.assertEqual(states('confirmed', captured=True), ('confirmed', 'completed'))

    def test_completed_booking_keeps_its_completed_payment(self):
        self.assertEqual(states('confirmed', 'completed', captured=True), ('completed', 'completed'))

    def test_confirmed_without_a_capture_is_not_a_completed_payment(self):
        self.assertEqual(states('confirmed'), ('confirmed', 'not_recorded'))
        self.assertEqual(states('confirmed', 'completed'), ('completed', 'not_recorded'))

    def test_pay_at_venue_walk_in_and_free(self):
        self.assertEqual(states('confirmed', offline=True), ('confirmed', 'at_venue'))
        self.assertEqual(states('confirmed', amount=0), ('confirmed', 'free'))

    def test_pending_and_abandoned_checkout(self):
        self.assertEqual(states('payment_pending'), ('pending', 'pending'))
        self.assertEqual(states('payment_pending', hold_expired=True), ('expired', 'unpaid'))
        # Event orders arrive with display status 'expired' already.
        self.assertEqual(states('payment_pending', 'expired'), ('expired', 'unpaid'))

    def test_failed_payment(self):
        self.assertEqual(states('failed'), ('not_confirmed', 'failed'))

    def test_cancellations(self):
        self.assertEqual(states('cancelled'), ('cancelled', 'unpaid'))
        # A PAID booking the customer cancelled: the money is still held.
        self.assertEqual(states('cancelled', captured=True), ('cancelled', 'completed'))

    def test_refunds(self):
        self.assertEqual(states('refund_pending', captured=True), ('cancelled', 'refund_pending'))
        self.assertEqual(states('refunded', captured=True), ('cancelled', 'refunded'))

    def test_removed_and_unknown(self):
        self.assertEqual(states('removed'), ('removed', 'unpaid'))
        self.assertEqual(states('mystery'), ('mystery', 'mystery'))

    def test_labels(self):
        fields = state_fields(('completed', 'refunded'))
        self.assertEqual(fields, {
            'bookingState': 'completed', 'bookingStateLabel': 'Completed',
            'paymentState': 'refunded', 'paymentStateLabel': 'Refunded',
        })

    def test_event_orders_follow_the_same_rules(self):
        order = SimpleNamespace(status='confirmed', display_status='completed', hold_expired=False,
                                razorpay_payment_id='pay_E', amount=500)
        self.assertEqual(order_states(order), ('completed', 'completed'))
        order.status, order.display_status = 'refunded', 'refunded'
        self.assertEqual(order_states(order), ('cancelled', 'refunded'))
        order.status, order.display_status, order.razorpay_payment_id = 'payment_pending', 'expired', ''
        self.assertEqual(order_states(order), ('expired', 'unpaid'))


@override_settings(
    RAZORPAY_KEY_ID='rzp_test_key',
    RAZORPAY_KEY_SECRET='test_key_secret',
    RAZORPAY_WEBHOOK_SECRET='test_webhook_secret',
)
class StateApiTests(BookingTestBase):
    order = base.PaymentFlowTests.order
    _sign = base.PaymentFlowTests._sign
    _webhook = base.PaymentFlowTests._webhook

    def pending(self):
        response = self.order()
        self.assertEqual(response.status_code, 201)
        return Booking.objects.get(pk=response.data['bookingId'])

    def verify(self, booking, payment_id='pay_OK', signature=None, **extra):
        return self.client.post('/api/payments/verify', {
            'bookingId': booking.id,
            'razorpay_order_id': booking.razorpay_order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature or self._sign(booking.razorpay_order_id, payment_id),
            **extra,
        }, format='json')

    def history(self):
        rows = self.client.get('/api/users/me/bookings').data['bookings']
        return {row['id']: row for row in rows}

    def test_verified_payment_confirms_and_completes_the_payment(self):
        booking = self.pending()
        response = self.verify(booking)
        self.assertEqual(response.status_code, 200)
        record = response.data['booking']
        self.assertEqual((record['bookingState'], record['paymentState']), ('confirmed', 'completed'))
        self.assertEqual(record['status'], 'confirmed')          # unchanged contract
        row = self.history()[booking.id]
        self.assertEqual((row['bookingState'], row['paymentState']), ('confirmed', 'completed'))

    def test_the_client_cannot_complete_a_payment(self):
        booking = self.pending()
        # A forged signature, plus fields a tampered client might add.
        response = self.verify(booking, signature='0' * 64, status='confirmed',
                               paymentState='completed', paid=True)
        self.assertEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'payment_pending')
        self.assertEqual(booking.state_fields()['paymentState'], 'pending')

    def test_retried_verify_is_idempotent_and_creates_no_duplicate(self):
        booking = self.pending()
        self.assertEqual(self.verify(booking).status_code, 200)
        self.assertEqual(self.verify(booking).status_code, 200)     # replay
        self._webhook('payment.captured')                           # fallback arrives too
        self.assertEqual(Booking.objects.filter(razorpay_order_id='order_TEST123').count(), 1)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')

    def test_completed_booking_reads_completed_on_both_questions_only_when_paid(self):
        booking = self.book().data['booking']
        Booking.objects.filter(pk=booking['id']).update(date=today_ist() - datetime.timedelta(days=1))
        row = self.history()[booking['id']]
        self.assertEqual((row['bookingState'], row['paymentState']), ('completed', 'completed'))
        # Same ended booking without a capture: completed booking, NOT paid.
        Booking.objects.filter(pk=booking['id']).update(razorpay_payment_id='')
        row = self.history()[booking['id']]
        self.assertEqual((row['bookingState'], row['paymentState']), ('completed', 'not_recorded'))

    def test_failed_payment_and_abandoned_checkout(self):
        booking = self.pending()
        self._webhook('payment.failed')
        booking.refresh_from_db()
        self.assertEqual(booking.state_fields()['bookingState'], 'not_confirmed')
        self.assertEqual(booking.state_fields()['paymentState'], 'failed')

        Booking.objects.filter(pk=booking.pk).update(
            status='payment_pending',
            created_at=booking.created_at - datetime.timedelta(hours=2),
            hold_expires_at=booking.created_at - datetime.timedelta(hours=2),
        )
        booking.refresh_from_db()
        self.assertEqual(booking.state_fields()['bookingState'], 'expired')
        self.assertEqual(booking.state_fields()['paymentState'], 'unpaid')

    def test_cancelled_and_refunded_bookings(self):
        booking = self.book().data['booking']
        Booking.objects.filter(pk=booking['id']).update(status='refunded', refund_id='rfnd_1')
        row = self.history()[booking['id']]
        self.assertEqual((row['bookingState'], row['paymentState']), ('cancelled', 'refunded'))
        self.assertEqual(row['bookingStateLabel'], 'Cancelled')
        self.assertEqual(row['paymentStateLabel'], 'Refunded')

    def test_redelivered_capture_on_a_refunded_booking_is_not_refunded_again(self):
        booking = self.pending()
        self._webhook('payment.captured')                 # confirms with pay_WH1
        Booking.objects.filter(pk=booking.pk).update(status='refunded', refund_id='rfnd_ADMIN')
        refund = MagicMock(status_code=500)
        with patch('bookings.razorpay_client.requests.post', return_value=refund) as post:
            response = self._webhook('payment.captured')  # Razorpay retries
        self.assertEqual(response.status_code, 200)
        post.assert_not_called()                          # no second refund attempt
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.refund_id), ('refunded', 'rfnd_ADMIN'))

    def test_admin_rows_carry_both_answers(self):
        from django.test import Client

        from accounts.models import User
        booking = self.book().data['booking']
        admin = User.objects.create_user(phone='9000000099', name='Admin', email='ad@example.com',
                                         role=User.Role.ADMIN, password='pw')
        client = Client()
        client.force_login(admin)
        rows = {r['id']: r for r in client.get('/api/admin/bootstrap').json()['bookings']}
        row = rows[booking['id']]
        self.assertEqual((row['bookingState'], row['paymentState']), ('confirmed', 'completed'))
        self.assertEqual(row['paymentStatus'], 'confirmed')        # raw value kept
