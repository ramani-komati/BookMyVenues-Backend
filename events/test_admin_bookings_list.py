"""
Event ticket orders on the super admin's main Bookings page.

The page is fed by GET /api/admin/bootstrap. Venue bookings come in
`bookings`; event ticket orders come in their own `eventOrders` key, built
with the SAME record the Events → Bookings modal uses. The rules under test:
  * every live order appears exactly once, with its persisted status
  * soft-removed orders stay out (as they do in the modal)
  * event money never leaks into `bookings` (dashboard/payout revenue)
  * the two admin views agree on id, amount and status for each order
  * only an authenticated super admin can read any of it
"""
import datetime

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from bookings.slots import today_ist
from bookings.views import PENDING_HOLD_MINUTES

from .models import EventOrder
from .tests import (
    EventTestCase,
    admin_client,
    auth,
    make_admin,
    make_customer,
    make_event,
    make_vendor,
)


class AdminBookingsListTests(EventTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.client = admin_client(self.admin)
        self.customer = make_customer()
        self.event = make_event(capacity=50)
        self.date = self.event.dates.first()
        self.venue_booking = Booking.objects.create(
            user=self.customer, date=today_ist() + datetime.timedelta(days=2),
            slots=['19:00 – 20:00'], amount=1220, customer_name='Asha',
            venue_name='Grand Hall', phone='9100000001',
        )

    def make_order(self, status, *, payment_id='', age_minutes=0, event=None,
                   name='Asha'):
        event = event or self.event
        event_date = event.dates.first()
        order = EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            event_name=event.name, date=event_date.date,
            start_time=event.start_time, end_time=event.end_time,
            customer_name=name, phone='9100000001',
            items=[{'key': 'adult', 'label': 'Adult', 'qty': 2, 'price': 500,
                    'seatsPerTicket': 1, 'subtotal': 1000}],
            seats=2, ticket_count=2, amount=1020, subtotal=1000, fee=20,
            status=status, razorpay_order_id=f'order_{status}_{age_minutes}',
            razorpay_payment_id=payment_id,
        )
        if age_minutes:
            EventOrder.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - datetime.timedelta(minutes=age_minutes)
            )
        return order

    def bootstrap(self, client=None):
        return (client or self.client).get('/api/admin/bootstrap')

    def by_id(self, rows):
        return {row['id']: row for row in rows}

    # ---- both kinds are listed -------------------------------------------

    def test_venue_bookings_still_listed_unchanged(self):
        self.make_order('confirmed', payment_id='pay_1')
        data = self.bootstrap().data
        self.assertEqual([b['id'] for b in data['bookings']], [self.venue_booking.id])
        row = data['bookings'][0]
        self.assertEqual(row['amountNum'], 1220)
        self.assertEqual(row['venue'], 'Grand Hall')
        self.assertEqual(row['status'], 'confirmed')

    def test_event_orders_appear_in_bootstrap(self):
        order = self.make_order('confirmed', payment_id='pay_1')
        rows = self.bootstrap().data['eventOrders']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['id'], order.id)
        self.assertEqual(row['kind'], 'event')
        self.assertEqual(row['eventName'], self.event.name)
        self.assertEqual(row['eventId'], str(self.event.id))
        self.assertEqual(row['customer'], 'Asha')
        self.assertEqual(row['phone'], '9100000001')
        self.assertEqual(row['date'], self.date.date.isoformat())
        self.assertEqual(row['startTime'], '18:00')
        self.assertEqual(row['endTime'], '22:00')
        self.assertEqual(row['items'][0]['qty'], 2)
        self.assertEqual(row['tickets'], 2)
        self.assertEqual(row['amount'], 1020)
        self.assertEqual(row['method'], 'online')
        self.assertTrue(row['paid'])

    def test_each_order_appears_once_and_never_in_venue_bookings(self):
        orders = [
            self.make_order('confirmed', payment_id='pay_a'),
            self.make_order('failed'),
            self.make_order('cancelled'),
        ]
        other = make_event(name='Other Night')
        orders.append(self.make_order('confirmed', payment_id='pay_b', event=other))
        data = self.bootstrap().data
        ids = [row['id'] for row in data['eventOrders']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {o.id for o in orders})
        # Event money must not inflate venue revenue figures.
        self.assertEqual([b['id'] for b in data['bookings']], [self.venue_booking.id])

    def test_soft_removed_orders_are_left_out(self):
        kept = self.make_order('failed')
        removed = self.make_order('cancelled')
        response = self.client.delete(
            reverse('admin-event-order', args=[self.event.id, removed.id])
        )
        self.assertEqual(response.status_code, 200)
        ids = [row['id'] for row in self.bootstrap().data['eventOrders']]
        self.assertEqual(ids, [kept.id])
        # Still in the database — the removal is soft.
        self.assertTrue(EventOrder.objects.filter(pk=removed.pk, status='removed').exists())

    # ---- statuses are the persisted ones ---------------------------------

    def test_statuses_are_reported_as_persisted(self):
        cases = {
            'pending': self.make_order('payment_pending'),
            'abandoned': self.make_order(
                'payment_pending', age_minutes=PENDING_HOLD_MINUTES + 5),
            'failed': self.make_order('failed'),
            'cancelled': self.make_order('cancelled'),
            'paid': self.make_order('confirmed', payment_id='pay_ok'),
            'refund_pending': self.make_order('refund_pending', payment_id='pay_rp'),
            'refunded': self.make_order('refunded', payment_id='pay_rf'),
        }
        rows = self.by_id(self.bootstrap().data['eventOrders'])
        expect = {
            # name: (paymentStatus, display status, paid?)
            'pending': ('payment_pending', 'payment_pending', False),
            'abandoned': ('payment_pending', 'expired', False),
            'failed': ('failed', 'failed', False),
            'cancelled': ('cancelled', 'cancelled', False),
            'paid': ('confirmed', 'confirmed', True),
            'refund_pending': ('refund_pending', 'refund_pending', False),
            'refunded': ('refunded', 'refunded', False),
        }
        for name, (raw, shown, paid) in expect.items():
            row = rows[cases[name].id]
            self.assertEqual(row['paymentStatus'], raw, name)
            self.assertEqual(row['status'], shown, name)
            self.assertEqual(row['paid'], paid, name)

    def test_an_initiated_but_unpaid_order_is_not_reported_as_paid(self):
        # A Razorpay order was created (payment initiated) but never captured.
        order = self.make_order('payment_pending')
        self.assertTrue(order.razorpay_order_id)
        row = self.bootstrap().data['eventOrders'][0]
        self.assertFalse(row['paid'])
        self.assertEqual(row['method'], '')
        self.assertEqual(row['paymentStatus'], 'payment_pending')

    def test_removability_matches_the_removal_rules(self):
        paid = self.make_order('confirmed', payment_id='pay_ok')
        live_checkout = self.make_order('payment_pending')
        failed = self.make_order('failed')
        refunded = self.make_order('refunded', payment_id='pay_rf')
        rows = self.by_id(self.bootstrap().data['eventOrders'])
        self.assertFalse(rows[paid.id]['removable'])
        self.assertFalse(rows[live_checkout.id]['removable'])
        self.assertFalse(rows[refunded.id]['removable'])
        self.assertTrue(rows[failed.id]['removable'])
        self.assertTrue(rows[paid.id]['removeBlockedReason'])

    # ---- the two admin views agree ---------------------------------------

    def test_bookings_page_and_event_modal_agree(self):
        for status, pay in (('confirmed', 'pay_1'), ('failed', ''),
                            ('payment_pending', ''), ('refunded', 'pay_2')):
            self.make_order(status, payment_id=pay)
        unified = self.by_id(self.bootstrap().data['eventOrders'])
        modal = self.by_id(self.client.get(
            reverse('admin-event-orders', args=[self.event.id])
        ).data['orders'])
        self.assertEqual(set(unified), set(modal))
        for order_id, row in modal.items():
            for key in ('amount', 'status', 'paymentStatus', 'paid',
                        'tickets', 'customer', 'date', 'removable'):
                self.assertEqual(unified[order_id][key], row[key], key)

    def test_orders_of_a_deleted_event_still_listed(self):
        order = self.make_order('confirmed', payment_id='pay_1')
        self.event.status = 'deleted'
        self.event.save()
        ids = [row['id'] for row in self.bootstrap().data['eventOrders']]
        self.assertEqual(ids, [order.id])

    # ---- access ----------------------------------------------------------

    def test_only_a_super_admin_can_read_event_orders(self):
        self.make_order('confirmed', payment_id='pay_1')
        for client in (APIClient(), auth(make_customer('9100000055')),
                       auth(make_vendor()),
                       admin_client(make_customer('9100000044'))):
            response = self.bootstrap(client)
            self.assertIn(response.status_code, (401, 403))
            self.assertNotIn('eventOrders', response.data)
