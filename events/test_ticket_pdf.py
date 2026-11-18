"""
PDF event tickets (events/tickets.py, events/ticket_pdf.py): generated only
after a verified payment, one identifiable ticket per ticket bought, QR codes
the existing Verify Booking flow accepts, attached to the confirmation email
exactly once however often the gateway retries, and available again from the
customer's history and the super admin's resend.

No email leaves the test run: the Resend transport (_send_email) is mocked
throughout, and RESEND_API_KEY is blank in the test settings anyway.
"""
import datetime
import re
from unittest import mock

from django.core.management import call_command
from django.test import override_settings

from adminpanel.models import AuditEntry
from bookings.slots import today_ist
from events import ticket_pdf, tickets
from events.models import EventOrder
from events.payments import _notify_confirmed, handle_event_webhook
from events.tests import (
    EventTestCase, admin_client, auth, make_admin, make_customer, make_event, make_vendor,
)

SEND = 'bookings.notifications._send_email'


def pdf_pages(content):
    return len(re.findall(rb'/Type\s*/Page[^s]', content))


@override_settings(NOTIFY_EMAIL_ENABLED=True, NOTIFY_SMS_ENABLED=False)
class TicketTestBase(EventTestCase):

    def setUp(self):
        super().setUp()
        self.vendor = make_vendor('9000000009', 'Gate Vendor')
        self.event = make_event(self.vendor, name='Garba Gala', days_ahead=(0,))
        self.asha = make_customer('9100000001', 'Asha Rao')
        self.ravi = make_customer('9100000002', 'Ravi')
        self.n = 0

    def order(self, status='confirmed', *, pay=True, adult=2, child=1, user=None):
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
        return EventOrder.objects.create(
            event=self.event, event_date=date, user=user or self.asha,
            event_name=self.event.name, event_category='Dandiya', date=date.date,
            location='Open Grounds', start_time=self.event.start_time,
            end_time=self.event.end_time, customer_name=(user or self.asha).name,
            phone='9100000001', items=items, seats=adult + child,
            ticket_count=adult + child, subtotal=subtotal, fee=20, amount=subtotal + 20,
            status=status, razorpay_order_id=f'order_t{self.n}',
            razorpay_payment_id=f'pay_t{self.n}' if pay else '',
        )


class TicketIdentityTests(TicketTestBase):

    def test_every_ticket_has_its_own_id_and_code(self):
        rows = tickets.tickets_for(self.order(adult=2, child=1))
        self.assertEqual([r['number'] for r in rows], [1, 2, 3])
        self.assertEqual([r['typeLabel'] for r in rows], ['Adult', 'Adult', 'Child'])
        ids = [r['ticketId'] for r in rows]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len({r['code'] for r in rows}), 3)
        other = tickets.tickets_for(self.order(adult=1, child=0))
        self.assertFalse(set(ids) & {r['ticketId'] for r in other})
        self.assertTrue(rows[0]['qr'].startswith(f'BMVT1:{rows[0]["ticketId"]}:'))

    def test_tickets_are_stable_so_a_rebuilt_pdf_matches_the_emailed_one(self):
        order = self.order()
        self.assertEqual(tickets.tickets_for(order), tickets.tickets_for(EventOrder.objects.get(pk=order.pk)))

    def test_unpaid_failed_cancelled_and_refunded_orders_have_no_tickets(self):
        for status, pay in (('payment_pending', False), ('failed', False), ('cancelled', False),
                            ('refund_pending', True), ('refunded', True), ('confirmed', False)):
            with self.subTest(status=status, pay=pay):
                order = self.order(status, pay=pay)
                self.assertEqual(tickets.tickets_for(order), [])
                with self.assertRaises(ticket_pdf.TicketUnavailable):
                    ticket_pdf.render(order, poster=False)


class TicketPdfContentTests(TicketTestBase):

    def test_pdf_has_one_page_per_ticket_with_the_order_details(self):
        order = self.order(adult=3, child=1)
        content = ticket_pdf.render(order, poster=False, compress=False)
        self.assertTrue(content.startswith(b'%PDF'))
        self.assertEqual(pdf_pages(content), 4)
        for text in ('Garba Gala', 'Asha Rao', order.id, f'{order.id}-01', f'{order.id}-04',
                     'Adult', 'Child', 'Rs. 1,770', 'Ticket 4 of 4', 'HAPVOO',
                     order.invoice_number):
            self.assertIn(text.encode(), content, text)
        code = tickets.tickets_for(order)[0]['code']
        self.assertIn(f'{code[:4]}-{code[4:]}'.encode(), content)

    def test_poster_is_only_fetched_from_our_own_storage(self):
        self.assertFalse(ticket_pdf._poster_allowed('https://evil.example.com/p.jpg'))
        self.assertFalse(ticket_pdf._poster_allowed('http://cdn.example.com/p.jpg'))
        with override_settings(SUPABASE_URL='https://abc.supabase.co'):
            self.assertTrue(ticket_pdf._poster_allowed('https://abc.supabase.co/storage/v1/x.jpg'))

    def test_a_poster_that_fails_to_load_never_stops_the_ticket(self):
        order = self.order(adult=1, child=0)
        with mock.patch('events.ticket_pdf.fetch_poster', side_effect=lambda url: None):
            self.assertTrue(ticket_pdf.render(order).startswith(b'%PDF'))


class QrVerificationTests(TicketTestBase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin()

    def verify(self, text):
        return admin_client(self.admin).post(
            '/api/admin/check-ins/verify', {'bookingId': text}, format='json',
        ).data

    def test_a_scanned_qr_verifies_and_names_the_ticket(self):
        order = self.order(adult=2, child=0)
        second = tickets.tickets_for(order)[1]
        result = self.verify(second['qr'])
        self.assertTrue(result['found'])
        self.assertEqual(result['booking']['id'], order.id)
        self.assertEqual(result['booking']['ticket']['ticketId'], second['ticketId'])
        self.assertEqual(result['code'], 'VALID')
        self.assertIn('Ticket 2 of 2', result['message'])

    def test_a_typed_ticket_id_also_works(self):
        order = self.order(adult=1, child=0)
        self.assertEqual(self.verify(f'{order.id.lower()}-01')['booking']['id'], order.id)

    def test_a_forged_or_altered_qr_is_refused(self):
        order = self.order(adult=2, child=0)
        real = tickets.tickets_for(order)[0]
        forged = real['qr'][:-1] + ('A' if real['qr'][-1] != 'A' else 'B')
        self.assertEqual(self.verify(forged)['code'], 'TICKET_NOT_GENUINE')
        # Re-pointing a genuine code at another ticket number fails too.
        moved = real['qr'].replace(f'{order.id}-01', f'{order.id}-02')
        self.assertEqual(self.verify(moved)['code'], 'TICKET_NOT_GENUINE')
        self.assertEqual(self.verify('BMVT1:garbage')['code'], 'TICKET_NOT_GENUINE')

    def test_a_ticket_number_beyond_the_order_is_invalid(self):
        order = self.order(adult=2, child=0)
        self.assertEqual(self.verify(f'{order.id}-03')['code'], 'INVALID_ID')

    def test_a_ticket_from_an_order_refunded_after_issue_no_longer_admits(self):
        order = self.order(adult=1, child=0)
        qr = tickets.tickets_for(order)[0]['qr']
        EventOrder.objects.filter(pk=order.pk).update(status='refunded')
        result = self.verify(qr)
        self.assertEqual(result['code'], 'REFUNDED')
        self.assertFalse(result['eligibleToday'])

    def test_the_vendor_gate_checks_in_the_order_from_a_ticket_qr(self):
        order = self.order(adult=3, child=0)
        qr = tickets.tickets_for(order)[2]['qr']
        gate = auth(self.vendor)
        body = {'bookingId': qr, 'eventId': str(self.event.id)}
        first = gate.post('/api/vendors/me/check-ins', body, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        result = first.data['result']
        self.assertEqual(result['code'], 'CHECKED_IN')
        self.assertEqual(result['booking']['ticket']['number'], 3)
        again = gate.post('/api/vendors/me/check-ins', body, format='json').data['result']
        self.assertEqual(again['code'], 'ALREADY_CHECKED_IN')


class TicketEmailTests(TicketTestBase):

    def confirm_by_webhook(self, order, times=1):
        entity = {'order_id': order.razorpay_order_id, 'id': f'pay_hook_{order.pk}',
                  'amount': order.amount * 100}
        for _ in range(times):
            with self.captureOnCommitCallbacks(execute=True):
                handle_event_webhook('payment.captured', entity)

    def customer_emails(self, send):
        return [c for c in send.call_args_list if c.args[0] == self.asha.email]

    def test_the_confirmation_email_carries_the_pdf_and_is_sent_once_despite_retries(self):
        order = self.order('payment_pending', pay=False, adult=2, child=0)
        with mock.patch(SEND) as send:
            self.confirm_by_webhook(order, times=3)
        mails = self.customer_emails(send)
        self.assertEqual(len(mails), 1)
        (name, data, mime), = mails[0].kwargs['attachments']
        self.assertEqual((name, mime), (f'tickets-{order.id}.pdf', 'application/pdf'))
        self.assertTrue(data.startswith(b'%PDF'))
        self.assertEqual(pdf_pages(data), 2)
        self.assertIn('attached as a PDF', mails[0].args[2])   # plain-text part says so
        order.refresh_from_db()
        self.assertIsNotNone(order.confirmation_sent_at)

    def test_a_failed_payment_gets_no_ticket_email(self):
        order = self.order('payment_pending', pay=False)
        with mock.patch(SEND) as send, self.captureOnCommitCallbacks(execute=True):
            handle_event_webhook('payment.failed', {'order_id': order.razorpay_order_id, 'id': 'pay_x'})
        send.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.status, 'failed')

    def test_if_the_pdf_fails_the_email_still_goes_and_says_where_to_get_tickets(self):
        order = self.order('payment_pending', pay=False)
        with mock.patch('events.ticket_pdf.render', side_effect=RuntimeError('boom')), \
                mock.patch(SEND) as send:
            self.confirm_by_webhook(order)
        mail, = self.customer_emails(send)
        self.assertNotIn('attachments', mail.kwargs)
        self.assertIn('could not attach', mail.args[2])

    def test_if_the_email_fails_it_can_be_retried_once_later(self):
        order = self.order('payment_pending', pay=False)
        with mock.patch(SEND, side_effect=RuntimeError('provider down')):
            self.confirm_by_webhook(order)
        order.refresh_from_db()
        self.assertEqual(order.status, 'confirmed')            # the payment stands
        self.assertIsNone(order.confirmation_sent_at)          # claim released
        with mock.patch(SEND) as send:
            call_command('send_pending_ticket_emails', verbosity=0, stdout=mock.MagicMock())
            call_command('send_pending_ticket_emails', verbosity=0, stdout=mock.MagicMock())
        self.assertEqual(len(self.customer_emails(send)), 1)
        order.refresh_from_db()
        self.assertIsNotNone(order.confirmation_sent_at)

    def test_the_retry_command_dry_run_sends_nothing(self):
        order = self.order('confirmed')
        with mock.patch(SEND) as send:
            out = mock.MagicMock()
            call_command('send_pending_ticket_emails', '--dry-run', stdout=out)
        send.assert_not_called()
        self.assertIn(order.id, out.write.call_args.args[0])

    def test_the_retry_command_ignores_old_orders(self):
        order = self.order('confirmed')
        EventOrder.objects.filter(pk=order.pk).update(
            created_at=order.created_at - datetime.timedelta(days=10))
        with mock.patch(SEND) as send:
            call_command('send_pending_ticket_emails', stdout=mock.MagicMock())
        send.assert_not_called()

    def test_notify_does_nothing_for_an_order_that_is_not_confirmed(self):
        order = self.order('payment_pending', pay=False)
        with mock.patch(SEND) as send:
            _notify_confirmed(order.pk)
        send.assert_not_called()


class ResendAndDownloadTests(TicketTestBase):

    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.panel = admin_client(self.admin)

    def resend(self, order, **body):
        return self.panel.post(f'/api/admin/event-orders/{order.id}/resend-tickets',
                               {'confirm': True, **body}, format='json')

    def test_resend_sends_the_pdf_to_the_customer_only_and_is_audited(self):
        order = self.order()
        with mock.patch(SEND) as send, mock.patch('bookings.notifications.send_otp_sms') as sms:
            response = self.resend(order)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], self.asha.email)
        self.assertIn('attachments', send.call_args.kwargs)
        sms.assert_not_called()
        self.assertTrue(AuditEntry.objects.filter(action='Resent event tickets', target_id=order.id).exists())

    def test_resend_needs_confirmation_and_has_a_cooldown(self):
        order = self.order()
        with mock.patch(SEND) as send:
            self.assertEqual(self.panel.post(
                f'/api/admin/event-orders/{order.id}/resend-tickets', {}, format='json').status_code, 400)
            self.assertEqual(self.resend(order).status_code, 200)
            second = self.resend(order)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.data['code'], 'COOLDOWN')
        self.assertEqual(send.call_count, 1)

    def test_resend_is_refused_for_an_unpaid_order(self):
        order = self.order('failed', pay=False)
        with mock.patch(SEND) as send:
            response = self.resend(order)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'NOT_ISSUABLE')
        send.assert_not_called()

    def test_a_failed_resend_is_reported_and_not_audited(self):
        order = self.order()
        with mock.patch(SEND, side_effect=RuntimeError('down')):
            response = self.resend(order)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(AuditEntry.objects.filter(action='Resent event tickets').exists())

    def test_only_the_admin_can_resend(self):
        order = self.order()
        self.assertIn(auth(self.asha).post(
            f'/api/admin/event-orders/{order.id}/resend-tickets', {'confirm': True}, format='json',
        ).status_code, (401, 403))

    def test_the_customer_can_download_their_own_tickets_again(self):
        order = self.order(adult=2, child=0)
        response = auth(self.asha).get(f'/api/users/me/event-orders/{order.id}/tickets.pdf')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn(f'tickets-{order.id}.pdf', response['Content-Disposition'])
        self.assertEqual(pdf_pages(response.content), 2)

    def test_another_customer_cannot_download_them(self):
        order = self.order()
        self.assertEqual(auth(self.ravi).get(
            f'/api/users/me/event-orders/{order.id}/tickets.pdf').status_code, 404)

    def test_no_pdf_for_an_unpaid_order_and_history_says_so(self):
        paid, unpaid = self.order(), self.order('payment_pending', pay=False)
        client = auth(self.asha)
        self.assertEqual(client.get(f'/api/users/me/event-orders/{unpaid.id}/tickets.pdf').status_code, 409)
        rows = {o['id']: o for o in client.get('/api/users/me/event-orders').data['orders']}
        self.assertTrue(rows[paid.id]['ticketPdf'])
        self.assertFalse(rows[unpaid.id]['ticketPdf'])
