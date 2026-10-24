"""
Deleting bookings from the super admin's main Bookings page.

DELETE /api/admin/bookings/<id>      — a venue booking
DELETE /api/admin/event-orders/<id>  — an event ticket order

The rules under test:
  * only an authenticated super admin can delete
  * only bookings that never took money qualify: unpaid holds past their
    window, failed attempts and cancelled checkouts. Confirmed, completed,
    refund-pending and refunded rows — and anything carrying a captured
    payment id — are refused with 409 and left exactly as they were
  * a live checkout (hold still open) is refused
  * deletion is SOFT and audited: the row stays in the database as
    'removed', leaves the list, the counts, availability, payouts, the
    vendor dashboard and the customer's list, and cannot be edited back
"""
import datetime
import uuid

from django.utils import timezone
from rest_framework.test import APIClient

from bookings.models import Booking
from bookings.slots import today_ist
from bookings.views import PENDING_HOLD_MINUTES
from events.models import EventOrder
from events.tests import (
    EventTestCase,
    admin_client,
    auth,
    make_admin,
    make_customer,
    make_event,
    make_vendor,
)
from venues.models import Listing

from .models import AuditEntry

OLD = PENDING_HOLD_MINUTES + 5


class BookingRemovalTests(EventTestCase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.client = admin_client(self.admin)
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall', name='Grand Hall',
            category='hall', record={'status': 'live'},
        )
        self.event = make_event(name='Garba Night')

    def venue(self, status, *, pay='', age=0, days=2, method='online', slots=None):
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer,
            date=today_ist() + datetime.timedelta(days=days),
            slots=slots or ['19:00 – 20:00'], amount=1220, fee=20,
            customer_name='Asha', venue_name='Grand Hall', phone='9100000001',
            status=status, razorpay_payment_id=pay, method=method,
            razorpay_order_id='order_v' if method == 'online' else '',
        )
        if age:
            Booking.objects.filter(pk=booking.pk).update(
                created_at=timezone.now() - datetime.timedelta(minutes=age)
            )
            booking.refresh_from_db()
        return booking

    def order(self, status, *, pay='', age=0):
        event_date = self.event.dates.first()
        order = EventOrder.objects.create(
            event=self.event, event_date=event_date, user=self.customer,
            event_name=self.event.name, date=event_date.date,
            customer_name='Asha', phone='9100000001',
            items=[{'key': 'adult', 'label': 'Adult', 'qty': 1, 'price': 500,
                    'seatsPerTicket': 1, 'subtotal': 500}],
            seats=1, ticket_count=1, subtotal=500, fee=20, amount=520,
            status=status, razorpay_payment_id=pay,
        )
        if age:
            EventOrder.objects.filter(pk=order.pk).update(
                created_at=timezone.now() - datetime.timedelta(minutes=age)
            )
        return order

    def delete_venue(self, booking, client=None, reason=None):
        return (client or self.client).delete(
            f'/api/admin/bookings/{booking.id}',
            {'reason': reason} if reason else None, format='json',
        )

    def delete_event(self, order, client=None):
        return (client or self.client).delete(f'/api/admin/event-orders/{order.id}')

    def bootstrap_ids(self):
        data = self.client.get('/api/admin/bootstrap').data
        return ({b['id'] for b in data['bookings']},
                {o['id'] for o in data['eventOrders']})

    # ---- what may go ------------------------------------------------------

    def test_unpaid_venue_bookings_can_be_deleted(self):
        for status, age in (('payment_pending', OLD), ('failed', 0), ('cancelled', 0)):
            with self.subTest(status=status):
                booking = self.venue(status, age=age)
                response = self.delete_venue(booking, reason='test cleanup')
                self.assertEqual(response.status_code, 200, response.data)
                self.assertEqual(response.data, {'removed': True, 'id': booking.id})
                booking.refresh_from_db()
                self.assertEqual(booking.status, 'removed')   # soft: still stored

    def test_unpaid_event_orders_can_be_deleted(self):
        for status, age in (('payment_pending', OLD), ('failed', 0), ('cancelled', 0)):
            with self.subTest(status=status):
                order = self.order(status, age=age)
                self.assertEqual(self.delete_event(order).status_code, 200)
                order.refresh_from_db()
                self.assertEqual(order.status, 'removed')

    # ---- what must stay ---------------------------------------------------

    def test_financial_venue_records_are_refused(self):
        cases = [
            ('confirmed', 'pay_1', 2, 'ACTIVE'),
            ('confirmed', 'pay_2', -3, 'ACTIVE'),      # completed
            ('refund_pending', 'pay_3', 2, 'ACTIVE'),
            ('refunded', 'pay_4', 2, 'REFUNDED'),
            ('cancelled', 'pay_5', 2, 'PAID'),         # paid, then cancelled
            ('failed', 'pay_6', 2, 'PAID'),
        ]
        for status, pay, days, code in cases:
            with self.subTest(status=status, pay=pay):
                booking = self.venue(status, pay=pay, days=days)
                response = self.delete_venue(booking)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.data['code'], code)
                booking.refresh_from_db()
                self.assertEqual(booking.status, status)   # untouched

    def test_walk_in_and_pay_at_venue_confirmed_bookings_are_refused(self):
        for method in ('walk-in', 'venue'):
            booking = self.venue('confirmed', method=method)
            self.assertEqual(self.delete_venue(booking).status_code, 409)

    def test_a_live_checkout_is_refused(self):
        booking = self.venue('payment_pending')            # hold still open
        response = self.delete_venue(booking)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'IN_CHECKOUT')
        order = self.order('payment_pending')
        self.assertEqual(self.delete_event(order).data['code'], 'IN_CHECKOUT')

    def test_paid_event_orders_are_refused(self):
        for status in ('confirmed', 'refund_pending', 'refunded'):
            order = self.order(status, pay='pay_e')
            self.assertEqual(self.delete_event(order).status_code, 409)
            order.refresh_from_db()
            self.assertEqual(order.status, status)

    # ---- authorization ----------------------------------------------------

    def test_only_a_super_admin_can_delete(self):
        booking = self.venue('cancelled')
        order = self.order('failed')
        for client in (APIClient(), auth(make_customer('9100000055')),
                       auth(self.vendor), admin_client(make_customer('9100000044'))):
            self.assertIn(self.delete_venue(booking, client).status_code, (401, 403))
            self.assertIn(self.delete_event(order, client).status_code, (401, 403))
        booking.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(booking.status, 'cancelled')
        self.assertEqual(order.status, 'failed')
        self.assertFalse(AuditEntry.objects.filter(action__startswith='Removed').exists())

    def test_unknown_and_already_removed_bookings_are_404(self):
        self.assertEqual(self.client.delete('/api/admin/bookings/bk_nope').status_code, 404)
        booking = self.venue('cancelled')
        self.delete_venue(booking)
        self.assertEqual(self.delete_venue(booking).status_code, 404)
        # ...and it cannot be edited back to life.
        response = self.client.patch(
            f'/api/admin/bookings/{booking.id}', {'status': 'confirmed'}, format='json'
        )
        self.assertEqual(response.status_code, 404)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'removed')

    # ---- audit and side effects ------------------------------------------

    def test_deletion_is_audited_with_the_short_reference(self):
        booking = self.venue('cancelled')
        self.delete_venue(booking, reason='duplicate checkout')
        entry = AuditEntry.objects.get(action='Removed venue booking')
        self.assertEqual(entry.target_id, booking.id)
        self.assertIn('#' + booking.id[3:][-6:].upper(), entry.target)
        self.assertIn('cancelled → removed', entry.change)
        self.assertIn('duplicate checkout', entry.change)

        order = self.order('failed')
        self.delete_event(order)
        entry = AuditEntry.objects.get(action='Removed event booking')
        self.assertIn(f'#{order.id}', entry.target)
        self.assertEqual(entry.target_id, order.id)

    def test_removed_rows_leave_the_list_and_counts(self):
        keep_v, gone_v = self.venue('confirmed', pay='pay_k'), self.venue('cancelled')
        keep_e, gone_e = self.order('confirmed', pay='pay_k'), self.order('failed')
        self.delete_venue(gone_v)
        self.delete_event(gone_e)
        venue_ids, event_ids = self.bootstrap_ids()
        self.assertEqual(venue_ids, {keep_v.id})
        self.assertEqual(event_ids, {keep_e.id})
        export = self.client.get('/api/admin/bookings/export')
        self.assertEqual(export.status_code, 200)
        self.assertNotIn(gone_v.id.encode(), export.content)

    def test_a_removed_booking_frees_its_slot(self):
        from bookings.views import _active_bookings
        booking = self.venue('cancelled')
        self.delete_venue(booking)
        self.assertFalse(_active_bookings(self.listing, booking.date).exists())

    def test_removed_bookings_vanish_for_customer_and_vendor(self):
        booking = self.venue('failed')
        self.delete_venue(booking)
        customer = auth(self.customer)
        ids = [b['id'] for b in customer.get('/api/users/me/bookings').data['bookings']]
        self.assertNotIn(booking.id, ids)
        self.assertEqual(customer.delete(f'/api/users/me/bookings/{booking.id}').status_code, 404)
        dashboard = auth(self.vendor).get('/api/vendors/me/dashboard').data
        self.assertNotIn(booking.id, {b['id'] for b in dashboard['allBookings']})

    def test_the_venue_route_never_removes_an_event_order(self):
        order = self.order('failed')
        self.assertEqual(self.client.delete(f'/api/admin/bookings/{order.id}').status_code, 404)
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')
