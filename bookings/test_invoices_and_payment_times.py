"""
Invoice numbers and payment transaction times (bookings/invoices.py,
bookings/payment_record.py).

The rules under test:
  * every venue booking and event order gets ONE invoice number, issued by
    the backend on insert, unique (database constraint), from one shared
    series, and never regenerated on read or update
  * concurrent bookings never share a number; a failed insert burns none
  * existing rows are numbered safely and repeatably (data migration /
    management command), oldest first, nothing else touched
  * paid_at is the time the SUCCESSFUL payment was recorded — distinct from
    created_at, never set for pending / failed / cancelled / walk-in rows,
    tied to the payment that succeeded after a retry, refined (never
    replaced) by Razorpay's own capture time
  * the fields are served only to the booking's customer, its vendor and the
    admin — never to another customer, another vendor or a public endpoint
"""
import datetime
import hashlib
import hmac
import io
import json
import threading
import unittest
import uuid
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from events.models import EventOrder
from events.tests import RZP_KEYS, auth, make_customer, make_event, signature
from venues.models import Listing

from . import invoices, payment_record
from .models import Booking, InvoiceSequence
from .slots import IST, today_ist
from .tests import RECORD, TOMORROW, BookingTestBase
from . import tests as base

INVOICE = r'^INV-\d{4}-\d{6}$'


def make_listing(vendor, name='Hall'):
    return Listing.objects.create(
        id=uuid.uuid4(), vendor=vendor, slug=f'{name.lower()}-{uuid.uuid4().hex[:6]}',
        record={**RECORD, 'id': 'x', 'status': 'live'}, name=name, category='hall',
    )


def raw_booking(listing, user=None, **fields):
    defaults = dict(listing=listing, user=user, venue_name=listing.name,
                    date=today_ist() + datetime.timedelta(days=1),
                    slots=['10:00 – 11:00'], amount=920, fee=20)
    defaults.update(fields)
    return Booking.objects.create(**defaults)


# =========================================================================
# Numbering
# =========================================================================

class InvoiceNumberFormatTests(TestCase):

    def test_financial_year_runs_april_to_march_in_ist(self):
        at = lambda *args: datetime.datetime(*args, tzinfo=IST)   # noqa: E731
        self.assertEqual(invoices.financial_year(at(2026, 4, 1, 0, 0)), '2627')
        self.assertEqual(invoices.financial_year(at(2027, 3, 31, 23, 59)), '2627')
        self.assertEqual(invoices.financial_year(at(2026, 3, 31, 23, 59)), '2526')
        # 31 Mar 19:00 UTC is already 1 April in India.
        utc = datetime.datetime(2026, 3, 31, 19, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(invoices.financial_year(utc), '2627')
        self.assertEqual(invoices.financial_year(at(2099, 12, 1, 0, 0)), '9900')

    def test_format_and_parse(self):
        self.assertEqual(invoices.format_invoice_number('2627', 123), 'INV-2627-000123')
        self.assertEqual(invoices.format_invoice_number('2627', 1234567), 'INV-2627-1234567')
        self.assertEqual(invoices.parse_invoice_number('INV-2627-000123'), ('2627', 123))
        for bad in ('', None, 'BMV-3F8A92', 'INV-26-1', 'inv-2627-000001'):
            self.assertIsNone(invoices.parse_invoice_number(bad), bad)


class InvoiceIssueTests(BookingTestBase):

    def test_a_new_venue_booking_gets_one_invoice_number(self):
        booking = raw_booking(self.listing, self.customer)
        self.assertRegex(booking.invoice_number, INVOICE)
        self.assertEqual(Booking.objects.get(pk=booking.pk).invoice_number, booking.invoice_number)

    def test_numbers_run_in_order_from_one_series(self):
        first = raw_booking(self.listing).invoice_number
        second = raw_booking(self.listing).invoice_number
        year, serial = invoices.parse_invoice_number(first)
        self.assertEqual(invoices.parse_invoice_number(second), (year, serial + 1))
        self.assertEqual(InvoiceSequence.objects.get(pk=year).last_number, serial + 1)

    def test_venue_bookings_and_event_orders_share_the_series(self):
        event = make_event()
        venue = raw_booking(self.listing)
        order = EventOrder.objects.create(event=event, date=today_ist(), amount=500)
        tail = lambda n: invoices.parse_invoice_number(n)[1]   # noqa: E731
        self.assertEqual(tail(order.invoice_number), tail(venue.invoice_number) + 1)

    def test_the_number_never_changes_on_update_read_or_status_change(self):
        booking = raw_booking(self.listing, self.customer, status='payment_pending')
        number = booking.invoice_number
        for status in ('failed', 'confirmed', 'refund_pending', 'refunded'):
            booking.status = status
            booking.save()
            booking.refresh_from_db()
            self.assertEqual(booking.invoice_number, number, status)
        # Reading the booking anywhere never issues a new one.
        for _ in range(3):
            self.client.get('/api/users/me/bookings')
            self.assertEqual(Booking.objects.get(pk=booking.pk).invoice_number, number)
        self.assertEqual(InvoiceSequence.objects.get().last_number,
                         invoices.parse_invoice_number(number)[1])

    def test_the_database_refuses_a_duplicate_number(self):
        taken = raw_booking(self.listing).invoice_number
        with self.assertRaises(IntegrityError), transaction.atomic():
            raw_booking(self.listing, invoice_number=taken)
        event = make_event()
        with self.assertRaises(IntegrityError), transaction.atomic():
            EventOrder.objects.create(event=event, date=today_ist(), invoice_number='INV-2627-777777')
            EventOrder.objects.create(event=event, date=today_ist(), invoice_number='INV-2627-777777')

    def test_a_failed_insert_burns_no_number_and_a_retry_gets_a_fresh_one(self):
        first = raw_booking(self.listing)
        doomed = Booking(listing=self.listing, venue_name='x', date=today_ist(),
                         amount=1, slots=[])
        doomed.id = first.id                                  # an id clash → insert fails
        doomed._generated_id = False
        with self.assertRaises(IntegrityError), transaction.atomic():
            doomed.save()
        self.assertIsNone(doomed.invoice_number)              # forgotten with the rollback
        after = raw_booking(self.listing)
        self.assertEqual(invoices.parse_invoice_number(after.invoice_number)[1],
                         invoices.parse_invoice_number(first.invoice_number)[1] + 1)

    def test_an_unpaid_booking_has_a_number_but_is_not_shown_as_paid(self):
        booking = raw_booking(self.listing, self.customer, status='payment_pending')
        record = booking.as_record()
        self.assertRegex(record['invoiceNumber'], INVOICE)
        self.assertIsNone(record['paidAt'])
        self.assertEqual(record['status'], 'payment_pending')


@unittest.skipUnless(connection.vendor == 'postgresql', 'needs real row locks (PostgreSQL)')
class ConcurrentInvoiceTests(TransactionTestCase):
    """Many bookings at the same instant: every one gets its own number."""

    def test_simultaneous_bookings_never_share_a_number(self):
        vendor = User.objects.create_user(phone='9000000771', name='V', role=User.Role.VENDOR)
        listing = make_listing(vendor)
        event = make_event(vendor, name='Race Night')
        workers = 8
        barrier = threading.Barrier(workers)
        numbers, errors = [], []

        def insert(index):
            from django.db import connection as thread_connection
            try:
                barrier.wait()
                if index % 2:
                    row = EventOrder.objects.create(event=event, date=today_ist(), amount=1)
                else:
                    row = raw_booking(listing, slots=[f'{6 + index}:00 – {7 + index}:00'])
                numbers.append(row.invoice_number)
            except Exception as error:      # surfaced by the assertion below
                errors.append(error)
            finally:
                thread_connection.close()

        threads = [threading.Thread(target=insert, args=(i,)) for i in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(numbers), workers)
        self.assertEqual(len(set(numbers)), workers)
        serials = sorted(invoices.parse_invoice_number(n)[1] for n in numbers)
        self.assertEqual(serials, list(range(1, workers + 1)))   # gap-free


class InvoiceClaimRaceTests(TestCase):
    """The SQLite-runnable half of the concurrency guarantee: the claim is a
    single conditional UPDATE, so even a caller that read a stale counter
    gets the next number, and the first claim of a new year survives losing
    the race to create the counter row."""

    def test_claims_are_increments_not_read_then_write(self):
        InvoiceSequence.objects.create(financial_year='2627', last_number=41)
        stale = InvoiceSequence.objects.get(pk='2627')            # read 41 …
        InvoiceSequence.objects.filter(pk='2627').update(last_number=42)   # … someone else claimed 42
        self.assertEqual(invoices._claim_serial(InvoiceSequence, '2627'), 43)
        self.assertEqual(stale.last_number, 41)

    def test_losing_the_race_to_open_a_new_year_still_claims_the_next_number(self):
        # Another request opened FY 27-28 (and took serial 1) between our
        # "no row yet" UPDATE and our INSERT: the INSERT hits the real primary
        # key, and we fall back to incrementing the winner's row.
        InvoiceSequence.objects.create(financial_year='2728', last_number=1)

        class FirstLookMisses:
            looks = 0

            def filter(self, **lookup):
                FirstLookMisses.looks += 1
                if FirstLookMisses.looks == 1:          # the row "isn't there yet"
                    return MagicMock(update=MagicMock(return_value=0))
                return InvoiceSequence.objects.filter(**lookup)

            def create(self, **fields):
                return InvoiceSequence.objects.create(**fields)

        racing = type('RacingSequence', (), {'objects': FirstLookMisses()})
        self.assertEqual(invoices._claim_serial(racing, '2728'), 2)
        self.assertEqual(InvoiceSequence.objects.get(pk='2728').last_number, 2)


# =========================================================================
# Existing rows
# =========================================================================

class BackfillTests(TestCase):

    def setUp(self):
        self.vendor = User.objects.create_user(phone='9000000661', name='V', role=User.Role.VENDOR)
        self.listing = make_listing(self.vendor)
        self.event = make_event(name='Old Night')

    def legacy(self, pk, created, **fields):
        """A row as it was before invoice numbers existed."""
        booking = raw_booking(self.listing, id=pk, razorpay_payment_id=f'pay_{pk}', **fields)
        Booking.objects.filter(pk=pk).update(invoice_number=None, created_at=created)
        return pk

    def legacy_order(self, pk, created):
        EventOrder.objects.create(id=pk, event=self.event, date=today_ist(), amount=1,
                                  razorpay_payment_id=f'pay_{pk}')
        EventOrder.objects.filter(pk=pk).update(invoice_number=None, created_at=created)
        return pk

    def reset_counter(self):
        InvoiceSequence.objects.all().delete()

    def test_existing_rows_are_numbered_oldest_first_by_financial_year(self):
        ist = lambda *a: datetime.datetime(*a, tzinfo=IST)   # noqa: E731
        self.legacy('bk_old2', ist(2026, 5, 2, 10, 0))
        self.legacy_order('eo_old1', ist(2026, 5, 1, 10, 0))
        self.legacy('bk_prev', ist(2026, 3, 30, 10, 0), status='refunded')
        self.legacy('wk_walk', ist(2026, 6, 1, 10, 0), walk_in=True, method='walk-in')
        self.reset_counter()

        self.assertEqual(invoices.assign_missing_invoice_numbers(), 4)

        number = lambda m, pk: m.objects.get(pk=pk).invoice_number   # noqa: E731
        self.assertEqual(number(Booking, 'bk_prev'), 'INV-2526-000001')
        self.assertEqual(number(EventOrder, 'eo_old1'), 'INV-2627-000001')
        self.assertEqual(number(Booking, 'bk_old2'), 'INV-2627-000002')
        self.assertEqual(number(Booking, 'wk_walk'), 'INV-2627-000003')
        # ids, payment references and statuses untouched; nothing created/deleted
        self.assertEqual(Booking.objects.get(pk='bk_prev').razorpay_payment_id, 'pay_bk_prev')
        self.assertEqual(Booking.objects.get(pk='bk_prev').status, 'refunded')
        self.assertEqual(Booking.objects.count(), 3)
        self.assertEqual(EventOrder.objects.count(), 1)
        self.assertIsNone(Booking.objects.get(pk='bk_old2').paid_at)   # never guessed

    def test_rerunning_changes_nothing_and_existing_numbers_are_kept(self):
        kept = raw_booking(self.listing).invoice_number
        self.legacy('bk_a', timezone.now())
        self.assertEqual(invoices.assign_missing_invoice_numbers(), 1)
        first_pass = dict(Booking.objects.values_list('pk', 'invoice_number'))
        self.assertEqual(invoices.assign_missing_invoice_numbers(), 0)
        self.assertEqual(dict(Booking.objects.values_list('pk', 'invoice_number')), first_pass)
        self.assertIn(kept, first_pass.values())

    def test_a_counter_behind_existing_numbers_is_caught_up_not_reused(self):
        existing = raw_booking(self.listing).invoice_number
        self.legacy('bk_b', timezone.now())
        self.reset_counter()                       # e.g. counter lost in a restore
        invoices.assign_missing_invoice_numbers()
        year, serial = invoices.parse_invoice_number(existing)
        self.assertEqual(Booking.objects.get(pk='bk_b').invoice_number,
                         invoices.format_invoice_number(year, serial + 1))
        self.assertRegex(raw_booking(self.listing).invoice_number, rf'-{serial + 2:06d}$')

    def test_blank_strings_count_as_missing(self):
        self.legacy('bk_blank', timezone.now())
        Booking.objects.filter(pk='bk_blank').update(invoice_number='')
        invoices.assign_missing_invoice_numbers()
        self.assertRegex(Booking.objects.get(pk='bk_blank').invoice_number, INVOICE)

    def test_management_command_dry_run_then_apply(self):
        self.legacy('bk_cmd', timezone.now())
        out = io.StringIO()
        call_command('assign_invoice_numbers', '--dry-run', stdout=out)
        self.assertIn('1 venue booking(s), 0 event order(s)', out.getvalue())
        self.assertIsNone(Booking.objects.get(pk='bk_cmd').invoice_number)
        call_command('assign_invoice_numbers', stdout=io.StringIO())
        self.assertRegex(Booking.objects.get(pk='bk_cmd').invoice_number, INVOICE)


# =========================================================================
# Payment transaction time — venue bookings
# =========================================================================

@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
                   RAZORPAY_WEBHOOK_SECRET='test_webhook_secret')
class VenuePaymentTimeTests(BookingTestBase):
    order = base.PaymentFlowTests.order
    _sign = base.PaymentFlowTests._sign

    def pending(self):
        response = self.order()
        self.assertEqual(response.status_code, 201)
        return Booking.objects.get(pk=response.data['bookingId'])

    def verify(self, booking, payment_id='pay_OK'):
        return self.client.post('/api/payments/verify', {
            'bookingId': booking.id, 'razorpay_order_id': booking.razorpay_order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': self._sign(booking.razorpay_order_id, payment_id),
        }, format='json')

    def webhook(self, event, payment_id='pay_OK', order_id='order_TEST123', created_at=None):
        payload = {'event': event,
                   'payload': {'payment': {'entity': {'id': payment_id, 'order_id': order_id}}}}
        if created_at is not None:
            payload['created_at'] = created_at
        body = json.dumps(payload)
        sig = hmac.new(b'test_webhook_secret', body.encode(), hashlib.sha256).hexdigest()
        return self.client.post('/api/payments/webhook', body,
                                content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=sig)

    def age(self, booking, minutes):
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=minutes))
        booking.refresh_from_db()

    def test_verified_checkout_records_a_payment_time_distinct_from_creation(self):
        booking = self.pending()
        self.age(booking, 7)                       # the customer took 7 minutes to pay
        self.assertEqual(self.verify(booking).status_code, 200)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.paid_at_source, payment_record.CHECKOUT)
        self.assertGreater(booking.paid_at - booking.created_at, datetime.timedelta(minutes=6))
        record = booking.as_record()
        self.assertNotEqual(record['paidAt'], record['createdAt'])
        self.assertEqual(record['paidAtSource'], 'checkout')

    def test_webhook_uses_razorpay_capture_time(self):
        booking = self.pending()
        captured = int(timezone.now().timestamp()) - 120
        self.webhook('payment.captured', created_at=captured)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')
        self.assertEqual(booking.paid_at,
                         datetime.datetime.fromtimestamp(captured, tz=datetime.timezone.utc))
        self.assertEqual(booking.paid_at_source, payment_record.GATEWAY)

    def test_webhook_without_a_usable_time_records_when_it_arrived(self):
        for bad in (None, 'soon', -5, 4102444800):          # missing, junk, negative, 2100
            booking = self.pending()
            before = timezone.now()
            self.webhook('payment.captured', order_id=booking.razorpay_order_id, created_at=bad)
            booking.refresh_from_db()
            self.assertEqual(booking.paid_at_source, payment_record.WEBHOOK, bad)
            self.assertGreaterEqual(booking.paid_at, before)
            Booking.objects.filter(pk=booking.pk).update(status='cancelled')   # free the slot

    def test_gateway_time_refines_the_checkout_time_for_the_same_payment(self):
        booking = self.pending()
        self.verify(booking)
        captured = int(timezone.now().timestamp()) - 30
        self.webhook('payment.captured', created_at=captured)      # redelivery after verify
        booking.refresh_from_db()
        self.assertEqual(booking.paid_at_source, payment_record.GATEWAY)
        self.assertEqual(int(booking.paid_at.timestamp()), captured)
        # …and a later server-side signal never replaces the gateway time.
        self.webhook('payment.captured')
        self.verify(booking)
        booking.refresh_from_db()
        self.assertEqual(int(booking.paid_at.timestamp()), captured)

    def test_a_different_payment_id_never_moves_the_time(self):
        booking = self.pending()
        self.verify(booking, 'pay_OK')
        stamped = Booking.objects.get(pk=booking.pk).paid_at
        self.webhook('payment.captured', payment_id='pay_OTHER',
                     created_at=int(timezone.now().timestamp()) - 3600)
        booking.refresh_from_db()
        self.assertEqual((booking.razorpay_payment_id, booking.paid_at), ('pay_OK', stamped))

    def test_an_old_confirmed_booking_is_not_given_a_guessed_time(self):
        booking = self.pending()
        Booking.objects.filter(pk=booking.pk).update(status='confirmed', razorpay_payment_id='pay_OK')
        self.webhook('payment.captured')                     # redelivery, no gateway time
        booking.refresh_from_db()
        self.assertIsNone(booking.paid_at)

    def test_pending_and_failed_bookings_have_no_payment_time(self):
        booking = self.pending()
        self.assertIsNone(booking.paid_at)
        self.webhook('payment.failed')
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'failed')
        self.assertIsNone(booking.paid_at)
        self.assertIsNone(booking.as_record()['paidAt'])

    def test_retry_after_failure_records_the_successful_payment(self):
        booking = self.pending()
        self.webhook('payment.failed', payment_id='pay_FAILED')
        self.assertEqual(self.verify(booking, 'pay_RETRY').status_code, 200)
        booking.refresh_from_db()
        self.assertEqual((booking.status, booking.razorpay_payment_id), ('confirmed', 'pay_RETRY'))
        self.assertIsNotNone(booking.paid_at)

    def test_capture_after_cancel_is_refunded_and_keeps_its_capture_time(self):
        booking = self.pending()
        Booking.objects.filter(pk=booking.pk).update(status='cancelled')
        refund = MagicMock(status_code=200)
        refund.json.return_value = {'id': 'rfnd_1'}
        none = MagicMock(status_code=200)
        none.json.return_value = {'items': []}
        captured = int(timezone.now().timestamp()) - 60
        with patch('bookings.razorpay_client.requests.post', return_value=refund), \
                patch('bookings.razorpay_client.requests.get', return_value=none):
            self.webhook('payment.captured', created_at=captured)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refunded')                      # never revived
        self.assertEqual(int(booking.paid_at.timestamp()), captured)

    def test_late_capture_on_a_resold_slot_is_refund_pending_with_its_time(self):
        booking = self.pending()
        self.age(booking, 31)                                # hold lapsed
        self.book()                                          # someone else took the slot
        self.assertEqual(self.verify(booking).status_code, 409)
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'refund_pending')
        self.assertEqual(booking.paid_at_source, payment_record.CHECKOUT)

    def test_walk_ins_have_no_online_payment_time(self):
        booking = raw_booking(self.listing, walk_in=True, method='walk-in', fee=0)
        self.assertIsNone(booking.as_record()['paidAt'])
        self.assertRegex(booking.as_record()['invoiceNumber'], INVOICE)


class PaymentRecordUnitTests(TestCase):

    def test_gateway_time_accepts_only_plausible_timestamps(self):
        now = int(timezone.now().timestamp())
        self.assertIsNotNone(payment_record.gateway_time({'created_at': now}))
        self.assertIsNotNone(payment_record.gateway_time({'created_at': str(now)}))
        for bad in (None, '', 'x', True, -1, 0, 1000, now + 3600, 10 ** 20, [now]):
            self.assertIsNone(payment_record.gateway_time({'created_at': bad}), bad)
        self.assertIsNone(payment_record.gateway_time('not a dict'))

    def test_nothing_is_stamped_without_the_matching_payment_id(self):
        row = Booking(razorpay_payment_id='')
        self.assertEqual(payment_record.stamp_payment_time(row, '', source='checkout'), [])
        row = Booking(razorpay_payment_id='pay_A')
        self.assertEqual(payment_record.stamp_payment_time(row, 'pay_B', source='gateway'), [])
        self.assertIsNone(row.paid_at)


# =========================================================================
# Payment transaction time — event orders
# =========================================================================

@RZP_KEYS
class EventPaymentTimeTests(TestCase):

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(capacity=50)
        date = self.event.dates.first()
        with patch('events.payments.create_order', return_value='order_EVT'):
            created = self.client.post(reverse('event-payment-order'), {
                'eventId': str(self.event.id), 'eventDateId': date.id, 'tickets': {'adult': 1},
            }, format='json')
        self.assertEqual(created.status_code, 201)
        self.order_id = created.data['eventOrderId']

    def order(self):
        return EventOrder.objects.get(pk=self.order_id)

    def webhook(self, event_name, payment_id='pay_E', created_at=None):
        payload = {'event': event_name,
                   'payload': {'payment': {'entity': {'id': payment_id, 'order_id': 'order_EVT'}}}}
        if created_at is not None:
            payload['created_at'] = created_at
        body = json.dumps(payload).encode()
        sig = hmac.new(b'test_webhook_secret', body, hashlib.sha256).hexdigest()
        return APIClient().post(reverse('payment-webhook'), data=body,
                                content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=sig)

    def test_an_event_order_gets_an_invoice_number_when_created(self):
        self.assertRegex(self.order().invoice_number, INVOICE)
        self.assertIsNone(self.order().paid_at)

    def test_verify_then_gateway_capture_time(self):
        with patch('events.notifications.notify_tickets_confirmed', return_value=True):
            response = self.client.post(reverse('event-payment-verify'), {
                'eventOrderId': self.order_id, 'razorpay_order_id': 'order_EVT',
                'razorpay_payment_id': 'pay_E', 'razorpay_signature': signature('order_EVT', 'pay_E'),
            }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['order']['paidAtSource'], 'checkout')
        self.assertRegex(response.data['order']['invoiceNumber'], INVOICE)
        captured = int(timezone.now().timestamp()) - 45
        self.webhook('payment.captured', created_at=captured)
        order = self.order()
        self.assertEqual((order.paid_at_source, int(order.paid_at.timestamp())), ('gateway', captured))

    def test_failed_then_retried_capture(self):
        self.webhook('payment.failed', payment_id='pay_BAD')
        self.assertEqual(self.order().status, 'failed')
        self.assertIsNone(self.order().paid_at)
        with patch('events.notifications.notify_tickets_confirmed', return_value=True):
            self.webhook('payment.captured', payment_id='pay_GOOD',
                         created_at=int(timezone.now().timestamp()))
        order = self.order()
        self.assertEqual((order.status, order.razorpay_payment_id), ('confirmed', 'pay_GOOD'))
        self.assertEqual(order.paid_at_source, 'gateway')

    def test_cancelled_order_capture_is_refunded_with_its_time(self):
        EventOrder.objects.filter(pk=self.order_id).update(status='cancelled')
        with patch('events.payments.refund_payment', return_value='rfnd_E'):
            self.webhook('payment.captured')
        order = self.order()
        self.assertEqual(order.status, 'refunded')
        self.assertEqual(order.paid_at_source, 'webhook')


# =========================================================================
# Who can see it
# =========================================================================

class VisibilityTests(BookingTestBase):
    """The invoice number and payment times are in every AUTHORISED booking
    response, and in no other."""

    def setUp(self):
        super().setUp()
        self.paid = self.book().data['booking']        # Asha's confirmed booking
        self.number = self.paid['invoiceNumber']
        self.other_vendor = User.objects.create_user(
            phone='9000000555', name='Other', role=User.Role.VENDOR)
        self.other_customer = User.objects.create_user(phone='9000000556', name='Ravi')

    def test_the_customer_sees_their_own(self):
        rows = self.client.get('/api/users/me/bookings').data['bookings']
        self.assertEqual([r['invoiceNumber'] for r in rows], [self.number])
        self.assertIsNotNone(rows[0]['paidAt'])
        self.assertIsNotNone(rows[0]['createdAt'])

    def test_another_customer_never_does(self):
        client = APIClient()
        client.force_authenticate(self.other_customer)
        response = client.get('/api/users/me/bookings')
        self.assertEqual(response.data['bookings'], [])
        self.assertNotIn(self.number, json.dumps(response.data))

    def test_the_venues_vendor_sees_it_and_another_vendor_does_not(self):
        client = APIClient()
        client.force_authenticate(self.vendor)
        mine = client.get('/api/vendors/me/dashboard').data['allBookings']
        self.assertEqual([b['invoiceNumber'] for b in mine], [self.number])
        self.assertIn('paidAt', mine[0])
        client.force_authenticate(self.other_vendor)
        theirs = client.get('/api/vendors/me/dashboard')
        self.assertNotIn(self.number, json.dumps(theirs.data))
        # nor can they act on it
        refused = client.post(f'/api/vendors/me/bookings/{self.paid["id"]}/collect', {}, format='json')
        self.assertEqual(refused.status_code, 403)

    def test_the_admin_sees_it_with_payment_references(self):
        from events.tests import admin_client, make_admin
        rows = admin_client(make_admin()).get(reverse('admin-bootstrap')).data['bookings']
        row = next(r for r in rows if r['id'] == self.paid['id'])
        self.assertEqual(row['invoiceNumber'], self.number)
        self.assertEqual(row['paidAtSource'], 'checkout')
        self.assertTrue(row['razorpayOrderId'].startswith('order_'))
        self.assertIsNotNone(row['paidAt'])
        self.assertIsNotNone(row['createdAt'])

    def test_customers_and_vendors_cannot_read_the_admin_feed(self):
        self.assertIn(self.client.get(reverse('admin-bootstrap')).status_code, (401, 403))

    def test_public_endpoints_never_carry_it(self):
        anon = APIClient()
        for url in (
            '/api/venues', f'/api/venues/{self.listing.slug}',
            f'/api/venues/{self.listing.id}/availability?date={TOMORROW}',
        ):
            response = anon.get(url)
            self.assertEqual(response.status_code, 200, url)
            body = json.dumps(response.data)
            self.assertNotIn(self.number, body, url)
            self.assertNotIn('invoiceNumber', body, url)
            self.assertNotIn('paidAt', body, url)


@RZP_KEYS
class EventVisibilityTests(TestCase):

    def setUp(self):
        from events.tests import make_vendor
        self.vendor = make_vendor('9000000441', 'Ev Vendor')
        self.other_vendor = make_vendor('9000000442', 'Rival')
        self.event = make_event(self.vendor, name='Vendor Night')
        self.customer = make_customer('9100000441')
        self.order = EventOrder.objects.create(
            event=self.event, user=self.customer, date=self.event.dates.first().date,
            event_date=self.event.dates.first(), amount=500, status='confirmed',
            razorpay_payment_id='pay_V', paid_at=timezone.now(), paid_at_source='gateway',
        )

    def test_owner_vendor_and_customer_see_it_others_do_not(self):
        url = reverse('vendor-event-orders', args=[self.event.id])
        rows = auth(self.vendor).get(url).data['orders']
        self.assertEqual(rows[0]['invoiceNumber'], self.order.invoice_number)
        self.assertEqual(rows[0]['paidAtSource'], 'gateway')
        self.assertEqual(auth(self.other_vendor).get(url).status_code, 404)

        mine = auth(self.customer).get(reverse('my-event-orders')).data['orders']
        self.assertEqual(mine[0]['invoiceNumber'], self.order.invoice_number)
        theirs = auth(make_customer('9100000442')).get(reverse('my-event-orders')).data['orders']
        self.assertEqual(theirs, [])

    def test_public_event_pages_never_carry_it(self):
        anon = APIClient()
        for url in ('/api/events', f'/api/events/{self.event.slug}',
                    f'/api/events/{self.event.id}/availability'):
            body = json.dumps(anon.get(url).data)
            self.assertNotIn(self.order.invoice_number, body, url)
            self.assertNotIn('invoiceNumber', body, url)
