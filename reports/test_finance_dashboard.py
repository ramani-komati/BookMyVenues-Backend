"""
The super admin Dashboard's money figures (reports/finance.py and
GET /api/admin/dashboard): only money actually received counts — venue
bookings and event tickets together — refunds come off, and every date range
returns figures that add up.
"""
import datetime
import uuid

from django.test import TestCase
from django.utils import timezone

from bookings.models import Booking
from bookings.slots import IST, today_ist
from events.models import EventOrder
from events.tests import admin_client, auth, make_admin, make_customer, make_event, make_vendor
from reports import finance
from venues.models import Listing


def ist_noon(day):
    return datetime.datetime.combine(day, datetime.time(12, 0), tzinfo=IST)


class FinanceBase(TestCase):
    def setUp(self):
        self.today = today_ist()
        self.admin = make_admin()
        self.vendor = make_vendor()
        self.asha = make_customer('9100000001', 'Asha')
        self.ravi = make_customer('9100000002', 'Ravi')
        self.hall = self.listing('Grand Hall', 'Party hall')
        self.turf = self.listing('City Turf', 'Play zone')
        self.pitch = self.listing('City Turf — Pitch 2', 'Play zone', unit_of=self.turf)
        self.event = make_event(name='Dandiya Night', capacity=None)
        self.n = 0

    def listing(self, name, category, unit_of=None):
        detail = {'unitOf': str(unit_of.id)} if unit_of else {}
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug=f'{uuid.uuid4().hex[:10]}',
            record={'name': name, 'category': category, 'detail': detail},
            name=name, category=category, status=Listing.Status.LIVE,
        )

    def booking(self, listing=None, status='confirmed', *, pay=True, amount=1000, user=None,
                method='upi', paid_at=None, refund_amount=None, walk_in=False,
                collected=False, pay_now=None, at_venue=0, payment_id=None):
        self.n += 1
        row = Booking.objects.create(
            listing=listing or self.hall, user=user or self.asha,
            venue_name=(listing or self.hall).name, category=(listing or self.hall).category,
            date=self.today + datetime.timedelta(days=3), slots=['19:00 – 20:00'],
            amount=amount, fee=20, status=status, method=method, walk_in=walk_in,
            razorpay_payment_id=(payment_id or f'pay_v{self.n}') if pay else '',
            razorpay_order_id=f'order_v{self.n}', refund_amount=refund_amount,
            collected=collected, collected_at=timezone.now() if collected else None,
            pay_now=pay_now, at_venue=at_venue,
        )
        if paid_at is not None:
            Booking.objects.filter(pk=row.pk).update(paid_at=paid_at)
            row.refresh_from_db()
        return row

    def order(self, status='confirmed', *, pay=True, adult=2, child=0, paid_at=None,
              refund_amount=None, user=None, payment_id=None):
        self.n += 1
        date = self.event.dates.first()
        items = []
        if adult:
            items.append({'key': 'adult', 'label': 'Adult', 'qty': adult, 'price': 500,
                          'seatsPerTicket': 1, 'subtotal': 500 * adult})
        if child:
            items.append({'key': 'child', 'label': 'Child', 'qty': child, 'price': 250,
                          'seatsPerTicket': 1, 'subtotal': 250 * child})
        subtotal = 500 * adult + 250 * child
        row = EventOrder.objects.create(
            event=self.event, event_date=date, user=user or self.asha,
            event_name=self.event.name, event_category='Dandiya', date=date.date,
            start_time=self.event.start_time, end_time=self.event.end_time,
            customer_name='Asha', items=items, seats=adult + child,
            ticket_count=adult + child, subtotal=subtotal, fee=20, amount=subtotal + 20,
            status=status, razorpay_order_id=f'order_e{self.n}',
            razorpay_payment_id=(payment_id or f'pay_e{self.n}') if pay else '',
            refund_amount=refund_amount,
        )
        if paid_at is not None:
            EventOrder.objects.filter(pk=row.pk).update(paid_at=paid_at)
            row.refresh_from_db()
        return row

    def figures(self, key='all', **dates):
        return finance.dashboard(finance.resolve_range(key, dates.get('start'), dates.get('end')))


class SuccessfulPaymentsTests(FinanceBase):

    def test_successful_venue_and_event_payments_are_both_included(self):
        self.booking(amount=1000)
        self.order(adult=2)                         # 1000 + 20 fee
        data = self.figures()
        self.assertEqual(data['venue']['online'], 1000)
        self.assertEqual(data['events']['online'], 1020)
        self.assertEqual(data['totals']['received'], 2020)
        self.assertEqual(data['totals']['net'], 2020)
        self.assertEqual(data['totals']['payments'], 2)

    def test_failed_pending_expired_and_cancelled_unpaid_are_excluded(self):
        self.booking(status='failed', pay=False)
        self.booking(status='payment_pending', pay=False)            # live hold
        expired = self.booking(status='payment_pending', pay=False)
        Booking.objects.filter(pk=expired.pk).update(
            created_at=timezone.now() - datetime.timedelta(hours=2), hold_expires_at=None,
        )
        self.booking(status='cancelled', pay=False)
        self.order(status='failed', pay=False)
        self.order(status='payment_pending', pay=False)
        self.order(status='cancelled', pay=False)
        data = self.figures()
        self.assertEqual(data['totals']['received'], 0)
        self.assertEqual(data['totals']['payments'], 0)
        counts = data['counts']['venue']
        self.assertEqual(
            (counts['failed'], counts['pendingPayment'], counts['expired'], counts['cancelled'], counts['paid']),
            (1, 1, 1, 1, 0),
        )
        events = data['counts']['events']
        self.assertEqual((events['failed'], events['pendingPayment'], events['cancelled'], events['paid']),
                         (1, 1, 1, 0))

    def test_a_confirmed_row_without_a_verified_payment_is_not_money(self):
        """A booking record (and its invoice number) is not proof of payment."""
        row = self.booking(status='confirmed', pay=False)
        self.assertTrue(row.invoice_number)
        data = self.figures()
        self.assertEqual(data['totals']['received'], 0)
        self.assertEqual(data['counts']['venue']['paid'], 0)

    def test_a_payment_id_on_a_failed_row_is_not_counted(self):
        self.booking(status='failed', pay=True)
        self.assertEqual(self.figures()['totals']['received'], 0)

    def test_confirmed_cash_collections_count_and_uncollected_cash_is_pending(self):
        self.booking(method='walk-in', walk_in=True, pay=False, amount=800, collected=True)
        self.booking(method='walk-in', walk_in=True, pay=False, amount=500, collected=False)
        data = self.figures()
        self.assertEqual(data['venue']['cash'], 800)
        self.assertEqual(data['totals']['received'], 800)
        self.assertEqual(data['totals']['pendingCollection'], 500)
        self.assertEqual(data['counts']['venue']['paid'], 1)
        self.assertEqual(data['counts']['venue']['awaitingCash'], 1)

    def test_part_payment_counts_the_online_share_and_only_collected_cash(self):
        self.booking(amount=1000, pay_now=300, at_venue=700, collected=False)
        self.booking(amount=1000, pay_now=300, at_venue=700, collected=True)
        data = self.figures()
        self.assertEqual(data['venue']['online'], 600)
        self.assertEqual(data['venue']['cash'], 700)
        self.assertEqual(data['totals']['pendingCollection'], 700)


class RefundTests(FinanceBase):

    def test_full_and_partial_refunds_are_subtracted_from_net(self):
        self.booking(status='refunded', amount=1000)                    # full (no amount recorded)
        self.booking(status='refunded', amount=1000, refund_amount=300)  # partial
        self.order(status='refunded', adult=1, refund_amount=None)      # 520 full
        data = self.figures()
        self.assertEqual(data['totals']['received'], 2520)
        self.assertEqual(data['venue']['refunded'], 1300)
        self.assertEqual(data['events']['refunded'], 520)
        self.assertEqual(data['totals']['refunded'], 1820)
        self.assertEqual(data['totals']['net'], 700)
        self.assertEqual(data['counts']['venue']['refunded'], 2)

    def test_refund_pending_is_received_and_listed_but_not_yet_refunded(self):
        self.booking(status='refund_pending', amount=900)
        data = self.figures()
        self.assertEqual(data['totals']['received'], 900)
        self.assertEqual(data['totals']['refundPending'], 900)
        self.assertEqual(data['totals']['refunded'], 0)

    def test_cancelled_after_payment_still_shows_the_money_held(self):
        self.booking(status='cancelled', amount=700)
        data = self.figures()
        self.assertEqual(data['totals']['received'], 700)
        self.assertEqual(data['counts']['venue']['cancelled'], 1)
        self.assertEqual(data['counts']['venue']['cancelledAfterPayment'], 1)

    def test_a_refund_can_never_exceed_the_captured_amount(self):
        self.booking(status='refunded', amount=500, refund_amount=9999)
        data = self.figures()
        self.assertEqual(data['totals']['refunded'], 500)
        self.assertEqual(data['totals']['net'], 0)


class DuplicateAndRetryTests(FinanceBase):

    def test_two_rows_carrying_the_same_payment_id_count_once(self):
        self.booking(amount=1000, payment_id='pay_same')
        self.booking(amount=1000, payment_id='pay_same')
        data = self.figures()
        self.assertEqual(data['venue']['online'], 1000)
        self.assertEqual(data['checks']['duplicatePaymentRows'], 1)

    def test_a_redelivered_capture_webhook_does_not_double_count_or_double_email(self):
        from unittest import mock

        from events.payments import handle_event_webhook

        order = self.order(status='payment_pending', pay=False, adult=4)
        entity = {'order_id': order.razorpay_order_id, 'id': 'pay_hook_1', 'amount': order.amount * 100}
        with mock.patch('events.notifications.notify_tickets_confirmed', return_value=True) as notify, \
                self.captureOnCommitCallbacks(execute=True):
            handle_event_webhook('payment.captured', entity)
        with mock.patch('events.notifications.notify_tickets_confirmed', return_value=True) as again, \
                self.captureOnCommitCallbacks(execute=True):
            handle_event_webhook('payment.captured', entity)
            handle_event_webhook('payment.captured', entity)
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(again.call_count, 0)
        data = self.figures()
        self.assertEqual(data['events']['online'], 2020)
        self.assertEqual(data['counts']['events']['paid'], 1)

    def test_a_retry_after_a_failed_attempt_is_one_payment(self):
        from unittest import mock

        from events.payments import handle_event_webhook

        order = self.order(status='payment_pending', pay=False, adult=1)
        handle_event_webhook('payment.failed', {'order_id': order.razorpay_order_id, 'id': 'pay_try1'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')
        self.assertEqual(self.figures()['totals']['received'], 0)
        with mock.patch('events.notifications.notify_tickets_confirmed', return_value=True), \
                self.captureOnCommitCallbacks(execute=True):
            handle_event_webhook('payment.captured', {
                'order_id': order.razorpay_order_id, 'id': 'pay_try2', 'amount': order.amount * 100,
            })
        data = self.figures()
        self.assertEqual(data['events']['online'], 520)
        self.assertEqual(data['events']['payments'], 1)
        self.assertEqual(data['counts']['events']['failed'], 0)   # the row is paid now


class OrdersVsTicketsTests(FinanceBase):

    def test_one_order_of_four_tickets_is_one_order_and_four_tickets(self):
        self.order(adult=3, child=1)
        counts = self.figures()['counts']['events']
        self.assertEqual(counts['total'], 1)
        self.assertEqual(counts['paid'], 1)
        self.assertEqual(counts['ticketsPaid'], 4)

    def test_tickets_on_unpaid_orders_are_not_counted(self):
        self.order(status='failed', pay=False, adult=5)
        self.assertEqual(self.figures()['counts']['events']['ticketsPaid'], 0)


class DateFilterTests(FinanceBase):

    def test_ranges_resolve_to_the_right_ist_days(self):
        t = datetime.date(2026, 11, 5)
        r = lambda key: finance.resolve_range(key, today=t)
        self.assertEqual((r('today').start, r('today').end), (t, t))
        self.assertEqual(r('yesterday').start, datetime.date(2026, 11, 4))
        self.assertEqual(r('7d').start, datetime.date(2026, 10, 30))
        self.assertEqual(r('30d').start, datetime.date(2026, 10, 7))
        self.assertEqual(r('month').start, datetime.date(2026, 11, 1))
        self.assertEqual((r('all').start, r('all').end), (None, None))
        custom = finance.resolve_range('custom', '2026-10-01', '2026-10-31', today=t)
        self.assertEqual((custom.start, custom.end), (datetime.date(2026, 10, 1), datetime.date(2026, 10, 31)))
        with self.assertRaises(finance.RangeError):
            finance.resolve_range('custom', '2026-10-31', '2026-10-01', today=t)
        with self.assertRaises(finance.RangeError):
            finance.resolve_range('custom', 'nope', '2026-10-01', today=t)
        with self.assertRaises(finance.RangeError):
            finance.resolve_range('fortnight', today=t)

    def test_payments_count_on_the_day_they_were_paid(self):
        yesterday = self.today - datetime.timedelta(days=1)
        self.booking(amount=1000, paid_at=ist_noon(self.today))
        self.booking(amount=400, paid_at=ist_noon(yesterday))
        self.order(adult=1, paid_at=ist_noon(yesterday))           # 520
        self.order(adult=1, paid_at=ist_noon(self.today - datetime.timedelta(days=40)))
        today = self.figures('today')
        self.assertEqual(today['totals']['received'], 1000)
        y = self.figures('yesterday')
        self.assertEqual((y['venue']['online'], y['events']['online']), (400, 520))
        self.assertEqual(self.figures('7d')['totals']['received'], 1920)
        self.assertEqual(self.figures('all')['totals']['received'], 2440)
        custom = self.figures('custom', start=yesterday.isoformat(), end=yesterday.isoformat())
        self.assertEqual(custom['totals']['received'], 920)
        # The cards, categories and daily series all agree for every range.
        for key in ('today', 'yesterday', '7d', '30d', 'month', 'all'):
            data = self.figures(key)
            self.assertEqual(sum(c['received'] for c in data['categories']), data['totals']['received'], key)
            self.assertEqual(sum(d['venue'] + d['event'] for d in data['daily']), data['totals']['received'], key)

    def test_the_endpoint_applies_the_range_and_refuses_bad_ones(self):
        self.booking(amount=1000, paid_at=ist_noon(self.today))
        client = admin_client(self.admin)
        ok = client.get('/api/admin/dashboard?range=today')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data['totals']['received'], 1000)
        self.assertEqual(ok.data['period']['key'], 'today')
        self.assertEqual(ok['Cache-Control'], 'no-store')
        bad = client.get('/api/admin/dashboard?range=custom&from=2026-10-10&to=2026-10-01')
        self.assertEqual(bad.status_code, 400)

    def test_only_the_super_admin_can_read_it(self):
        self.assertIn(auth(self.asha).get('/api/admin/dashboard').status_code, (401, 403))
        self.assertIn(self.client.get('/api/admin/dashboard').status_code, (401, 403))


class CategoryRevenueTests(FinanceBase):

    def test_categories_are_built_from_successful_payments_only(self):
        self.booking(self.hall, amount=3000)
        self.booking(self.pitch, amount=1000)          # a pitch counts as Play zone
        self.booking(self.turf, status='failed', pay=False, amount=50000)
        self.order(adult=2)                            # Dandiya event, 1020
        rows = {r['label']: r for r in self.figures()['categories']}
        self.assertEqual(rows['Party hall']['received'], 3000)
        self.assertEqual(rows['Play zone']['received'], 1000)
        self.assertEqual(rows['Dandiya']['received'], 1020)
        self.assertEqual(rows['Dandiya']['source'], 'event')
        self.assertAlmostEqual(sum(r['percent'] for r in rows.values()), 100, delta=0.2)

    def test_category_refunds_and_net(self):
        self.booking(self.hall, status='refunded', amount=2000, refund_amount=500)
        row = self.figures()['categories'][0]
        self.assertEqual((row['received'], row['refunded'], row['net']), (2000, 500, 1500))


class SharedDefinitionTests(FinanceBase):
    """The bootstrap's venue revenue and a customer's total spent use the
    same rules as the Dashboard."""

    def test_bootstrap_rows_use_the_shared_definitions(self):
        self.booking(self.turf, amount=1000)
        self.booking(self.pitch, amount=500)                        # folds into City Turf
        self.booking(self.turf, status='failed', pay=False, amount=9000)
        self.booking(self.turf, status='refunded', amount=800, refund_amount=200)
        self.order(adult=1)                                          # Asha's ticket, 520
        self.order(status='payment_pending', pay=False, adult=3)
        data = admin_client(self.admin).get('/api/admin/bootstrap').data
        turf = next(v for v in data['venues'] if v['name'] == 'City Turf')
        self.assertEqual(turf['revenueNum'], 1000 + 500 + 800 - 200)
        asha = next(u for u in data['users'] if u['id'] == self.asha.id)
        self.assertEqual(asha['spentNum'], 1000 + 500 + 600 + 520)
        self.assertEqual(asha['eventOrders'], 2)
        vendor = next(v for v in data['vendors'] if v['id'] == self.vendor.id)
        self.assertEqual(vendor['earningsNum'], 1000 + 500 + 600)
        dash = self.figures()
        self.assertEqual(dash['venue']['net'], turf['revenueNum'])
        self.assertEqual(dash['totals']['net'], asha['spentNum'])
