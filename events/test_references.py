"""
Short event order IDs ("K7M2QX") — the primary key IS the booking ID.

The rules under test:
  * every NEW order's primary key is 6 uppercase letters/digits from a
    secure generator, never all-hex (so it can never equal a venue booking's
    "#3F8A92"-style reference) and without the look-alikes 0/O/1/I/L
  * uniqueness is the primary key's own database constraint; a clash —
    pre-checked, or raised by the constraint mid-insert — is retried with a
    fresh id, while an explicit duplicate id and any other integrity error
    still fail exactly as Django normally does
  * an existing order is never re-keyed, overwritten or re-saved under a new
    id, and orders created before short IDs keep their 'eo_…' ids
  * both id shapes work end to end: checkout, verify, the webhook, customer
    cancel, the customer / vendor / admin records, admin removal, emails/SMS
  * no separate `reference` column exists
"""
import hashlib
import hmac
import json
import re
from unittest.mock import patch

from django.db import IntegrityError
from django.db.models.query import QuerySet
from django.urls import reverse

from .models import ORDER_ID_ALPHABET, ORDER_ID_LENGTH, EventOrder, make_order_id
from .tests import (
    RZP_KEYS,
    EventTestCase,
    admin_client,
    auth,
    make_admin,
    make_customer,
    make_event,
    signature,
)

SHORT = re.compile(r'^[A-Z0-9]{6}$')
HEX_ONLY = re.compile(r'^[0-9A-F]+$')
LEGACY_ID = 'eo_3f8a92c1d04e'


def make_order(event, customer, **over):
    event_date = event.dates.first()
    fields = dict(
        event=event, event_date=event_date, user=customer,
        event_name=event.name, date=event_date.date,
        start_time=event.start_time, end_time=event.end_time,
        customer_name='Asha', phone=customer.phone,
        items=[{'key': 'adult', 'label': 'Adult', 'qty': 1, 'price': 500,
                'seatsPerTicket': 1, 'subtotal': 500}],
        seats=1, ticket_count=1, subtotal=500, fee=20, amount=520,
        status='confirmed', razorpay_payment_id='pay_1',
    )
    fields.update(over)
    return EventOrder.objects.create(**fields)


class OrderIdGenerationTests(EventTestCase):

    def setUp(self):
        super().setUp()
        self.customer = make_customer()
        self.event = make_event(name='Garba Night')

    def test_new_orders_get_a_six_character_primary_key(self):
        order = make_order(self.event, self.customer)
        self.assertRegex(order.id, SHORT)
        self.assertEqual(len(order.id), ORDER_ID_LENGTH)
        self.assertEqual(order.pk, order.id)
        self.assertTrue(EventOrder.objects.filter(pk=order.id).exists())

    def test_no_separate_reference_column(self):
        names = {f.name for f in EventOrder._meta.get_fields()}
        self.assertNotIn('reference', names)
        self.assertNotIn('reference', make_order(self.event, self.customer).as_record())

    def test_ids_avoid_look_alikes_and_never_look_like_a_venue_reference(self):
        for _ in range(3000):
            code = make_order_id()
            self.assertRegex(code, SHORT)
            self.assertTrue(set(code) <= set(ORDER_ID_ALPHABET))
            self.assertFalse(set(code) & set('01OIL'), code)
            self.assertFalse(HEX_ONLY.match(code), code)

    def test_ids_are_unique(self):
        ids = [make_order(self.event, self.customer).id for _ in range(150)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_an_id_already_in_use_is_skipped(self):
        taken = make_order(self.event, self.customer).id
        with patch('bookings.booking_codes.new_code', side_effect=[taken, 'K7M2QX']):
            order = EventOrder(**self.fields())
            order.id = taken                     # as if the default drew it
            order._generated_id = True
            order.save()
        self.assertEqual(order.id, 'K7M2QX')
        self.assertEqual(EventOrder.objects.count(), 2)

    def test_a_clash_raised_by_the_constraint_is_retried(self):
        # Another request takes the id between the pre-check and the INSERT.
        taken = make_order(self.event, self.customer)
        before = EventOrder.objects.get(pk=taken.pk)
        real_exists = QuerySet.exists
        calls = {'n': 0}

        def exists(qs):
            calls['n'] += 1
            # The pre-check asks the registry, venue bookings and event
            # orders; all three answer 'free' this once.
            return False if calls['n'] <= 3 else real_exists(qs)

        with patch('bookings.booking_codes.new_code', return_value='P3RT9W'), \
             patch.object(QuerySet, 'exists', exists):
            order = EventOrder(**self.fields())
            order.id = taken.id
            order._generated_id = True
            order.save()
        self.assertEqual(order.id, 'P3RT9W')
        # The existing order was neither overwritten nor re-keyed.
        after = EventOrder.objects.get(pk=taken.pk)
        self.assertEqual((after.customer_name, after.amount, after.created_at),
                         (before.customer_name, before.amount, before.created_at))

    def test_gives_up_loudly_when_no_free_id_is_found(self):
        taken = make_order(self.event, self.customer).id
        with patch('bookings.booking_codes.new_code', return_value=taken):
            order = EventOrder(**self.fields())
            order.id = taken
            order._generated_id = True
            with self.assertRaises(IntegrityError):
                order.save()
        self.assertEqual(EventOrder.objects.count(), 1)

    def test_an_explicit_duplicate_id_is_never_silently_changed(self):
        first = make_order(self.event, self.customer)
        with self.assertRaises(IntegrityError):
            make_order(self.event, self.customer, id=first.id)

    def test_saving_an_existing_order_keeps_its_id(self):
        order = make_order(self.event, self.customer)
        original = order.id
        order.status = 'refund_pending'
        order.save(update_fields=['status'])
        order.save()
        loaded = EventOrder.objects.get(pk=original)
        loaded.save()
        self.assertEqual(EventOrder.objects.get().id, original)

    def test_loaded_orders_are_not_marked_generated(self):
        make_order(self.event, self.customer)
        self.assertFalse(EventOrder.objects.get()._generated_id)

    def test_existing_long_ids_are_untouched(self):
        legacy = make_order(self.event, self.customer, id=LEGACY_ID)
        new = make_order(self.event, self.customer)
        self.assertEqual(legacy.id, LEGACY_ID)
        self.assertEqual(EventOrder.objects.get(pk=LEGACY_ID).customer_name, 'Asha')
        legacy.status = 'refunded'
        legacy.save()
        self.assertEqual(set(EventOrder.objects.values_list('pk', flat=True)),
                         {LEGACY_ID, new.id})

    def fields(self):
        event_date = self.event.dates.first()
        return dict(
            event=self.event, event_date=event_date, user=self.customer,
            event_name=self.event.name, date=event_date.date,
            customer_name='Ravi', phone=self.customer.phone, items=[],
            seats=1, ticket_count=1, subtotal=500, fee=20, amount=520,
            status='payment_pending',
        )


@RZP_KEYS
class BothIdShapesEndToEndTests(EventTestCase):
    """A new short-id order and an older 'eo_…' order through every path."""

    def setUp(self):
        super().setUp()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(name='Garba Night', capacity=100)
        self.date = self.event.dates.first()
        self.admin = admin_client(make_admin())

    def checkout(self, gateway_order):
        with patch('events.payments.create_order', return_value=gateway_order):
            response = self.client.post(
                reverse('event-payment-order'),
                {'eventId': str(self.event.id), 'eventDateId': self.date.id,
                 'tickets': {'adult': 2}},
                format='json',
            )
        self.assertEqual(response.status_code, 201, response.data)
        return EventOrder.objects.get(pk=response.data['eventOrderId'])

    def legacy_checkout(self, gateway_order):
        """An order exactly as one placed before short ids was stored."""
        order = self.checkout(gateway_order)
        EventOrder.objects.filter(pk=order.pk).update(id=LEGACY_ID)
        return EventOrder.objects.get(pk=LEGACY_ID)

    def verify(self, order, payment_id='pay_1'):
        return self.client.post(reverse('event-payment-verify'), {
            'eventOrderId': order.id,
            'razorpay_order_id': order.razorpay_order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature(order.razorpay_order_id, payment_id),
        }, format='json')

    def webhook(self, event, order, payment_id='pay_WH'):
        payload = json.dumps({'event': event, 'payload': {'payment': {'entity': {
            'id': payment_id, 'order_id': order.razorpay_order_id,
            'amount': order.amount * 100,
        }}}})
        sig = hmac.new(b'test_webhook_secret', payload.encode(), hashlib.sha256).hexdigest()
        return self.client.post('/api/payments/webhook', payload,
                                content_type='application/json',
                                HTTP_X_RAZORPAY_SIGNATURE=sig)

    def test_checkout_returns_the_short_id(self):
        order = self.checkout('order_new1')
        self.assertRegex(order.id, SHORT)

    def test_verify_confirms_both_shapes(self):
        for order in (self.checkout('order_new2'), self.legacy_checkout('order_old2')):
            with self.subTest(id=order.id), patch('events.notifications._send', return_value=True):
                response = self.verify(order)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data['order']['id'], order.id)
                order.refresh_from_db()
                self.assertEqual(order.status, 'confirmed')

    def test_webhook_confirms_and_fails_both_shapes(self):
        for make, gw in ((self.checkout, 'order_new3'), (self.legacy_checkout, 'order_old3')):
            order = make(gw)
            with self.subTest(id=order.id):
                self.webhook('payment.failed', order)
                order.refresh_from_db()
                self.assertEqual(order.status, 'failed')
                with patch('events.notifications._send', return_value=True):
                    self.webhook('payment.captured', order)   # retry succeeded
                order.refresh_from_db()
                self.assertEqual(order.status, 'confirmed')
                EventOrder.objects.filter(pk=order.pk).update(status='refunded')

    def test_customer_can_cancel_both_shapes(self):
        for order in (self.checkout('order_new4'), self.legacy_checkout('order_old4')):
            with self.subTest(id=order.id):
                url = reverse('cancel-event-order', args=[order.id])
                self.assertEqual(self.client.delete(url).status_code, 200)
                order.refresh_from_db()
                self.assertEqual(order.status, 'cancelled')

    def test_records_everywhere_use_the_actual_id(self):
        new = self.checkout('order_new5')
        old = self.legacy_checkout('order_old5')
        mine = {o['id'] for o in self.client.get(reverse('my-event-orders')).data['orders']}
        self.assertEqual(mine, {new.id, old.id})
        boot = {o['id'] for o in self.admin.get('/api/admin/bootstrap').data['eventOrders']}
        self.assertEqual(boot, {new.id, old.id})
        modal = self.admin.get(reverse('admin-event-orders', args=[self.event.id])).data
        self.assertEqual({o['id'] for o in modal['orders']}, {new.id, old.id})

    def test_admin_can_remove_both_shapes(self):
        for order in (self.checkout('order_new6'), self.legacy_checkout('order_old6')):
            with self.subTest(id=order.id):
                EventOrder.objects.filter(pk=order.pk).update(status='failed')
                response = self.admin.delete(f'/api/admin/event-orders/{order.id}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data['id'], order.id)

    def test_every_notification_quotes_the_actual_id(self):
        for order in (self.checkout('order_new7'), self.legacy_checkout('order_old7')):
            with self.subTest(id=order.id):
                with patch('events.notifications._send', return_value=True) as send, \
                     self.captureOnCommitCallbacks(execute=True):
                    self.verify(order)
                bodies = [str(c.args[2]) + str(c.kwargs.get('html', ''))
                          for c in send.call_args_list]
                self.assertTrue(bodies)
                for body in bodies:
                    self.assertIn(order.id, body)
