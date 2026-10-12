"""
Event order payment states, confirmation email, booking history and the
super-admin "remove booking" action.

The rules under test:
  * only a VERIFIED payment (checkout signature or signed webhook) confirms
  * a failed attempt is 'failed', not 'cancelled', and stays confirmable —
    Razorpay lets the customer retry inside the same checkout
  * a dismissed checkout is 'cancelled' and a late capture is refunded
  * the confirmation email goes out once, after commit, never for unpaid
  * admin removal is a soft remove of UNPAID orders only
"""
import datetime
import hashlib
import hmac
import json
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from adminpanel.models import AuditEntry
from bookings.views import PENDING_HOLD_MINUTES

from .inventory import seats_remaining
from .models import EventOrder
from .tests import (
    RZP_KEYS,
    EventTestCase,
    admin_client,
    auth,
    make_admin,
    make_customer,
    make_event,
    make_vendor,
    signature,
)

GATEWAY_ORDER = 'order_State1'
NOTIFY = 'events.notifications.notify_tickets_confirmed'


@RZP_KEYS
class PaymentStateTests(EventTestCase):
    """Checkout callback + webhook, driven through the real endpoints."""

    def setUp(self):
        super().setUp()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(capacity=100, address='Warangal',
                                maps_link='https://maps.app.goo.gl/abc')
        self.date = self.event.dates.first()
        with patch('events.payments.create_order', return_value=GATEWAY_ORDER):
            created = self.client.post(
                reverse('event-payment-order'),
                {'eventId': str(self.event.id), 'eventDateId': self.date.id,
                 'tickets': {'adult': 2, 'child': 1}},
                format='json',
            )
        self.assertEqual(created.status_code, 201)
        self.order_id = created.data['eventOrderId']

    # --- helpers ---------------------------------------------------------

    def order(self):
        return EventOrder.objects.get(pk=self.order_id)

    def verify(self, payment_id='pay_1', sig=None):
        return self.client.post(
            reverse('event-payment-verify'),
            {'eventOrderId': self.order_id, 'razorpay_order_id': GATEWAY_ORDER,
             'razorpay_payment_id': payment_id,
             'razorpay_signature': sig or signature(GATEWAY_ORDER, payment_id)},
            format='json',
        )

    def webhook(self, event_name, payment_id='pay_1', amount=None):
        entity = {'id': payment_id, 'order_id': GATEWAY_ORDER}
        if amount is not None:
            entity['amount'] = amount
        body = json.dumps({'event': event_name,
                           'payload': {'payment': {'entity': entity}}}).encode()
        sig = hmac.new(b'test_webhook_secret', body, hashlib.sha256).hexdigest()
        return APIClient().post(
            reverse('payment-webhook'), data=body,
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=sig,
        )

    def paid_amount(self):
        return self.order().amount * 100

    # --- 1. success ------------------------------------------------------

    def test_verified_payment_confirms_and_emails_once(self):
        with patch(NOTIFY, return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.verify()
        self.assertEqual(response.status_code, 200)
        order = self.order()
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(order.razorpay_payment_id, 'pay_1')
        self.assertIsNotNone(order.confirmation_sent_at)
        notify.assert_called_once()

    # --- 2. failed -------------------------------------------------------

    def test_failed_payment_is_failed_not_a_ticket(self):
        with patch(NOTIFY, return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.webhook('payment.failed')
        self.assertEqual(self.order().status, 'failed')
        self.assertEqual(seats_remaining(self.date), 100)     # seats released
        notify.assert_not_called()
        history = self.client.get(reverse('my-event-orders')).data['orders']
        self.assertEqual([o['status'] for o in history], ['failed'])

    def test_retry_after_a_failed_attempt_can_still_be_confirmed(self):
        # The production bug: attempt 1 fails (webhook), attempt 2 succeeds
        # in the same checkout. It used to be refused and auto-refunded.
        self.webhook('payment.failed', payment_id='pay_fail')
        with patch(NOTIFY, return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.verify(payment_id='pay_ok')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.order().status, 'confirmed')
        notify.assert_called_once()

    def test_capture_webhook_after_a_failed_attempt_confirms_not_refunds(self):
        self.webhook('payment.failed', payment_id='pay_fail')
        with patch('events.payments.refund_payment') as refund:
            self.webhook('payment.captured', payment_id='pay_ok',
                         amount=self.paid_amount())
        refund.assert_not_called()
        self.assertEqual(self.order().status, 'confirmed')

    def test_a_late_failure_never_downgrades_a_confirmed_order(self):
        self.verify()
        self.webhook('payment.failed', payment_id='pay_old_attempt')
        self.assertEqual(self.order().status, 'confirmed')

    # --- 3. pending ------------------------------------------------------

    def test_pending_payment_stays_pending(self):
        self.assertEqual(self.order().status, 'payment_pending')
        history = self.client.get(reverse('my-event-orders')).data['orders']
        self.assertEqual(history[0]['status'], 'payment_pending')
        self.assertIsNone(self.order().confirmation_sent_at)

    def test_a_lapsed_hold_reads_expired_but_is_still_confirmable(self):
        EventOrder.objects.filter(pk=self.order_id).update(
            created_at=timezone.now()
            - datetime.timedelta(minutes=PENDING_HOLD_MINUTES + 1)
        )
        history = self.client.get(reverse('my-event-orders')).data['orders']
        self.assertEqual(history[0]['status'], 'expired')
        self.assertEqual(self.order().status, 'payment_pending')   # not failed
        # A verified capture that arrives late still turns into tickets.
        self.webhook('payment.captured', amount=self.paid_amount())
        self.assertEqual(self.order().status, 'confirmed')

    # --- 4. cancelled checkout -------------------------------------------

    def test_cancelled_checkout_is_never_marked_paid(self):
        cancel = self.client.delete(
            reverse('cancel-event-order', args=[self.order_id])
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(self.order().status, 'cancelled')
        with patch(NOTIFY, return_value=True) as notify:
            response = self.verify()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.order().status, 'cancelled')
        notify.assert_not_called()

    def test_dismissing_after_a_failed_attempt_keeps_it_failed(self):
        self.webhook('payment.failed')
        cancel = self.client.delete(
            reverse('cancel-event-order', args=[self.order_id])
        )
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(self.order().status, 'failed')

    def test_a_failure_reported_after_the_dismissal_reads_failed(self):
        # Webhooks are async: the widget dismissal usually lands first.
        self.client.delete(reverse('cancel-event-order', args=[self.order_id]))
        self.webhook('payment.failed')
        self.assertEqual(self.order().status, 'failed')

    # --- 5. invalid signature --------------------------------------------

    def test_invalid_signature_is_rejected(self):
        with patch(NOTIFY, return_value=True) as notify:
            response = self.verify(sig='f' * 64)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.order().status, 'payment_pending')
        notify.assert_not_called()

    def test_unsigned_webhook_is_rejected(self):
        body = json.dumps({'event': 'payment.captured', 'payload': {'payment': {
            'entity': {'id': 'pay_1', 'order_id': GATEWAY_ORDER,
                       'amount': self.paid_amount()}}}})
        response = APIClient().post(
            reverse('payment-webhook'), data=body,
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE='bad',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.order().status, 'payment_pending')

    # --- 6 + 8. duplicates: one ticket, one email -------------------------

    def test_duplicate_verify_and_webhook_send_one_email(self):
        with patch(NOTIFY, return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.verify()
            with self.captureOnCommitCallbacks(execute=True):
                self.verify()
            with self.captureOnCommitCallbacks(execute=True):
                self.webhook('payment.captured', amount=self.paid_amount())
            with self.captureOnCommitCallbacks(execute=True):
                self.webhook('payment.captured', amount=self.paid_amount())
        notify.assert_called_once()
        self.assertEqual(
            EventOrder.objects.filter(status='confirmed').count(), 1
        )

    def test_webhook_first_then_verify_sends_one_email(self):
        with patch(NOTIFY, return_value=True) as notify:
            with self.captureOnCommitCallbacks(execute=True):
                self.webhook('payment.captured', amount=self.paid_amount())
            with self.captureOnCommitCallbacks(execute=True):
                response = self.verify()
        self.assertEqual(response.status_code, 200)
        notify.assert_called_once()

    def test_a_failed_send_releases_the_claim_for_a_retry(self):
        with patch(NOTIFY, return_value=False):
            with self.captureOnCommitCallbacks(execute=True):
                self.verify()
        self.assertEqual(self.order().status, 'confirmed')
        self.assertIsNone(self.order().confirmation_sent_at)

    def test_redelivered_capture_for_a_refunded_order_does_not_refund_again(self):
        self.client.delete(reverse('cancel-event-order', args=[self.order_id]))
        with patch('events.payments.refund_payment', return_value='rfnd_1') as refund:
            self.webhook('payment.captured', amount=self.paid_amount())
            self.webhook('payment.captured', amount=self.paid_amount())
        refund.assert_called_once()
        self.assertEqual(self.order().status, 'refunded')

    def test_amount_mismatch_confirms_nothing(self):
        self.webhook('payment.captured', amount=100)
        self.assertEqual(self.order().status, 'payment_pending')


@RZP_KEYS
class ConfirmationEmailContentTests(EventTestCase):
    """What the customer actually receives."""

    def test_email_carries_the_booking_details(self):
        customer = make_customer()
        event = make_event(name='Garba Night', address='3-4-69, Sudhanagar',
                           maps_link='https://maps.app.goo.gl/abc')
        event_date = event.dates.first()
        order = EventOrder.objects.create(
            event=event, event_date=event_date, user=customer,
            event_name=event.name, location=event.location,
            date=event_date.date, start_time=event.start_time,
            end_time=event.end_time,
            items=[
                {'key': 'adult', 'label': 'Adult', 'qty': 2, 'price': 500,
                 'subtotal': 1000},
                {'key': 'child', 'label': 'Child', 'qty': 1, 'price': 250,
                 'subtotal': 250},
            ],
            seats=3, ticket_count=3, subtotal=1250, fee=20, amount=1270,
            status='confirmed', razorpay_payment_id='pay_1',
        )
        from .notifications import notify_tickets_confirmed
        with patch('events.notifications._send', return_value=True) as send:
            self.assertTrue(notify_tickets_confirmed(order))

        email = next(c for c in send.call_args_list
                     if c.args[0] == 'email' and c.args[1] == customer.email)
        text, html = email.args[2], email.kwargs['html']
        self.assertIn('Garba Night', email.kwargs['subject'])
        for needle in ('Garba Night', f'{event_date.date:%d %b %Y}', '18:00',
                       '2 × Adult', '1 × Child', '1,270', order.id,
                       'Open Grounds', 'Sudhanagar', '/bookings'):
            self.assertIn(needle, text, needle)
        for needle in ('Garba Night', '2 × Adult', '1,270', order.id,
                       'maps.app.goo.gl'):
            self.assertIn(needle, html, needle)


class BookingHistoryTests(EventTestCase):
    """GET /users/me/event-orders shows every outcome with its own status."""

    def test_each_status_is_reported_as_itself(self):
        customer = make_customer()
        event = make_event()
        event_date = event.dates.first()

        def order(status):
            return EventOrder.objects.create(
                event=event, event_date=event_date, user=customer,
                event_name=event.name, date=event_date.date,
                start_time=event.start_time, end_time=event.end_time,
                seats=1, ticket_count=1, amount=520, subtotal=500, fee=20,
                status=status,
            )

        expected = {}
        for status in ('confirmed', 'payment_pending', 'failed', 'cancelled',
                       'refund_pending', 'refunded'):
            expected[order(status).id] = status
        hidden = order('removed')

        rows = auth(customer).get(reverse('my-event-orders')).data['orders']
        got = {row['id']: row['status'] for row in rows}
        self.assertEqual(got, expected)
        self.assertNotIn(hidden.id, got)


class AdminRemoveEventOrderTests(EventTestCase):
    """DELETE /api/admin/events/<id>/orders/<order_id>."""

    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.client = admin_client(self.admin)
        self.customer = make_customer()
        self.event = make_event(capacity=50)
        self.date = self.event.dates.first()

    def make_order(self, status, *, payment_id='', age_minutes=0, event=None):
        event = event or self.event
        event_date = event.dates.first()
        order = EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            event_name=event.name, date=event_date.date,
            seats=2, ticket_count=2, amount=1020, subtotal=1000, fee=20,
            status=status, razorpay_order_id=f'order_{status}_{age_minutes}',
            razorpay_payment_id=payment_id,
        )
        if age_minutes:
            EventOrder.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - datetime.timedelta(minutes=age_minutes)
            )
        return order

    def remove(self, order, *, client=None, event_id=None):
        return (client or self.client).delete(
            reverse('admin-event-order',
                    args=[event_id or self.event.id, order.id]),
        )

    def listing(self):
        return self.client.get(
            reverse('admin-event-orders', args=[self.event.id])
        ).data

    def test_admin_can_remove_a_failed_booking(self):
        paid = self.make_order('confirmed', payment_id='pay_ok')
        failed = self.make_order('failed')
        before = self.listing()
        self.assertTrue(
            next(r for r in before['orders'] if r['id'] == failed.id)['removable']
        )

        response = self.remove(failed)
        self.assertEqual(response.status_code, 200)
        failed.refresh_from_db()
        self.assertEqual(failed.status, 'removed')     # soft: row still exists

        after = self.listing()
        self.assertEqual([r['id'] for r in after['orders']], [paid.id])
        self.assertEqual(after['total'], 1)
        # Totals are confirmed money only — untouched by the removal.
        self.assertEqual(after['revenue'], before['revenue'])
        self.assertEqual(after['ticketsSold'], before['ticketsSold'])
        self.assertTrue(
            AuditEntry.objects.filter(action='Removed event booking',
                                      target_id=failed.id).exists()
        )

    def test_cancelled_and_abandoned_bookings_are_removable(self):
        cancelled = self.make_order('cancelled')
        abandoned = self.make_order(
            'payment_pending', age_minutes=PENDING_HOLD_MINUTES + 5
        )
        self.assertEqual(self.remove(cancelled).status_code, 200)
        self.assertEqual(self.remove(abandoned).status_code, 200)

    def test_paid_bookings_cannot_be_removed(self):
        for status in ('confirmed', 'refund_pending'):
            order = self.make_order(status, payment_id=f'pay_{status}')
            before = self.listing()['revenue']
            response = self.remove(order)
            self.assertEqual(response.status_code, 409, status)
            self.assertEqual(response.data['code'], 'PAID')
            order.refresh_from_db()
            self.assertEqual(order.status, status)
            self.assertEqual(order.razorpay_payment_id, f'pay_{status}')
            self.assertEqual(self.listing()['revenue'], before)

    def test_refunded_bookings_are_kept_as_records(self):
        order = self.make_order('refunded', payment_id='pay_r')
        response = self.remove(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'REFUNDED')
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')

    def test_an_order_with_a_captured_payment_is_never_removed(self):
        order = self.make_order('cancelled', payment_id='pay_odd')
        self.assertEqual(self.remove(order).status_code, 409)

    def test_a_live_checkout_cannot_be_removed(self):
        order = self.make_order('payment_pending')
        response = self.remove(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'IN_CHECKOUT')

    def test_the_order_must_belong_to_the_event_in_the_url(self):
        other_event = make_event(name='Other Night')
        order = self.make_order('failed', event=other_event)
        response = self.remove(order)            # URL says self.event
        self.assertEqual(response.status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')

    def test_unauthorised_users_cannot_remove(self):
        order = self.make_order('failed')
        customer_jwt = auth(make_customer('9100000055'))
        vendor_jwt = auth(make_vendor())
        for client in (APIClient(), customer_jwt, vendor_jwt,
                       admin_client(make_customer('9100000044'))):
            response = self.remove(order, client=client)
            self.assertIn(response.status_code, (401, 403))
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')

    @RZP_KEYS
    def test_a_late_capture_for_a_removed_order_is_refunded(self):
        order = self.make_order('cancelled')
        self.remove(order)
        body = json.dumps({'event': 'payment.captured', 'payload': {'payment': {
            'entity': {'id': 'pay_late', 'order_id': order.razorpay_order_id,
                       'amount': 102000}}}}).encode()
        sig = hmac.new(b'test_webhook_secret', body, hashlib.sha256).hexdigest()
        with patch('events.payments.refund_payment', return_value='rfnd_9') as refund:
            APIClient().post(
                reverse('payment-webhook'), data=body,
                content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=sig,
            )
        refund.assert_called_once()
        order.refresh_from_db()
        self.assertEqual(order.status, 'refunded')
