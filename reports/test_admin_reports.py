"""
GET /api/admin/exports/<kind> — the super-admin panel's Excel exports.

  * super admin only (session + IsAdmin); vendors, customers and anonymous
    callers are refused; unknown kinds 404; bad filters 400
  * each export lists the rows its page lists, with the page's filters,
    across every page of a paginated list
  * user/vendor exports carry no credentials
  * money columns follow the real payment records: unpaid, cancelled and
    refunded bookings are never counted as revenue; refunds use the
    recorded amount
  * every export is audited and changes nothing
"""
import datetime
from unittest.mock import patch

from django.utils import timezone

from adminpanel.models import AuditEntry
from bookings.models import Booking
from events.models import EventOrder
from events.tests import EventTestCase, admin_client, auth, make_customer
from venues.models import Listing

from .fixtures import World, headers, raw_xml, read_sheets

URL = '/api/admin/exports/{}'


class AdminExportTestCase(EventTestCase):

    def setUp(self):
        super().setUp()
        self.w = World()
        self.client = admin_client(self.w.admin)

    def export(self, kind, client=None, **params):
        return (client or self.client).get(URL.format(kind), params)

    def sheets(self, kind, **params):
        response = self.export(kind, **params)
        self.assertEqual(response.status_code, 200, getattr(response, 'data', response.content[:200]))
        return read_sheets(response)

    def rows(self, kind, sheet, **params):
        return self.sheets(kind, **params)[sheet]


class AccessTests(AdminExportTestCase):

    KINDS = ('users', 'vendors', 'venues', 'approvals', 'events', 'payouts',
             'refunds', 'payments', 'audit')

    def test_every_export_downloads_for_a_super_admin(self):
        for kind in self.KINDS:
            response = self.export(kind)
            self.assertEqual(response.status_code, 200, kind)
            self.assertTrue(response.content.startswith(b'PK'), kind)
            self.assertEqual(response['Cache-Control'], 'no-store')
            day = self.w.today.isoformat()
            self.assertRegex(response['Content-Disposition'],
                             rf'attachment; filename="admin_[a-z_]+_{day}\.xlsx"')

    def test_file_names(self):
        day = self.w.today.isoformat()
        expected = {
            'users': 'admin_users', 'vendors': 'admin_vendors', 'payments': 'admin_payments',
            'events': 'admin_events', 'audit': 'admin_audit_log',
        }
        for kind, prefix in expected.items():
            self.assertIn(f'{prefix}_{day}.xlsx', self.export(kind)['Content-Disposition'])

    def test_anonymous_is_refused(self):
        from rest_framework.test import APIClient
        for kind in self.KINDS:
            self.assertIn(self.export(kind, client=APIClient()).status_code, (401, 403), kind)

    def test_vendor_and_customer_tokens_are_refused(self):
        for user in (self.w.vendor, self.w.asha):
            for kind in self.KINDS:
                self.assertIn(self.export(kind, client=auth(user)).status_code, (401, 403))

    def test_a_logged_in_non_admin_session_is_refused(self):
        for user in (self.w.vendor, self.w.asha):
            self.assertEqual(self.export('users', client=admin_client(user)).status_code, 403)

    def test_unknown_export_is_404(self):
        self.assertEqual(self.export('passwords').status_code, 404)

    def test_bad_filters_are_refused(self):
        for kind, params in (
            ('users', {'status': 'deleted'}),
            ('vendors', {'kyc': 'maybe'}),
            ('venues', {'status': 'bogus'}),
            ('events', {'tab': 'everything'}),
            ('payments', {'from': '2026-10-10', 'to': '2026-10-01'}),
            ('payments', {'from': 'yesterday'}),
            ('users', {'q': 'x' * 101}),
        ):
            response = self.export(kind, **params)
            self.assertEqual(response.status_code, 400, (kind, params))
            self.assertIn('detail', response.json())

    def test_every_export_is_audited_with_its_filters(self):
        self.export('users', q='asha', status='active')
        entry = AuditEntry.objects.filter(action='Exported users').get()
        self.assertEqual(entry.target, '1 users')
        self.assertIn("q='asha'", entry.change)
        self.assertIn('status=active', entry.change)
        self.export('payments')
        self.assertTrue(AuditEntry.objects.filter(action='Exported payments report').exists())

    def test_oversized_export_is_refused_not_truncated(self):
        with patch('reports.common.MAX_ROWS', 1):
            response = self.export('users')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Narrow the filters', response.json()['detail'])

    def test_exports_change_nothing(self):
        before = (sorted(Booking.objects.values_list('id', 'status', 'amount')),
                  sorted(EventOrder.objects.values_list('id', 'status', 'amount')),
                  sorted(Listing.objects.values_list('id', 'status')))
        for kind in self.KINDS:
            self.export(kind)
        after = (sorted(Booking.objects.values_list('id', 'status', 'amount')),
                 sorted(EventOrder.objects.values_list('id', 'status', 'amount')),
                 sorted(Listing.objects.values_list('id', 'status')))
        self.assertEqual(before, after)


class UsersExportTests(AdminExportTestCase):

    def test_columns(self):
        self.assertEqual(headers(self.export('users'), 'Users'), [
            'User ID', 'Name', 'Email', 'Phone', 'Role', 'Vendor Access', 'Account Status',
            'Registration Date', 'Venue Bookings (listed)', 'Confirmed Venue Bookings',
            'Confirmed Venue Booking Value', 'Paid Event Orders', 'Paid Event Order Value',
            'Refunded Bookings & Orders',
        ])

    def test_lists_the_users_page_set(self):
        # Customer identities only — vendors and the admin never booked.
        ids = {r['User ID'] for r in self.rows('users', 'Users')}
        self.assertEqual(ids, {self.w.asha.id, self.w.ravi.id})

    def test_a_vendor_who_also_books_is_a_user(self):
        self.w.vendor.is_customer = True
        self.w.vendor.save(update_fields=['is_customer'])
        row = next(r for r in self.rows('users', 'Users') if r['User ID'] == self.w.vendor.id)
        self.assertEqual(row['Role'], 'Vendor')
        self.assertEqual(row['Vendor Access'], 'Yes')

    def test_no_credentials_anywhere_in_the_file(self):
        response = self.export('users')
        for header in headers(response, 'Users'):
            for word in ('password', 'token', 'otp', 'hash', 'secret'):
                self.assertNotIn(word, header.lower())
        xml = raw_xml(response)
        self.assertNotIn(self.w.asha.password, xml)
        self.assertNotIn('pbkdf2', xml)

    def test_values_and_money(self):
        asha = next(r for r in self.rows('users', 'Users') if r['User ID'] == self.w.asha.id)
        self.assertEqual(asha['Name'], 'Asha')
        self.assertEqual(asha['Phone'], '9100000001')
        self.assertEqual(asha['Email'], '9100000001@example.com')
        self.assertEqual(asha['Account Status'], 'Active')
        self.assertIsInstance(asha['Registration Date'], datetime.datetime)
        self.assertEqual(asha['Venue Bookings (listed)'], 11)     # every non-removed row
        # Only bookings that still stand: unpaid, cancelled, refunded are out.
        self.assertEqual(asha['Confirmed Venue Bookings'], 4)
        self.assertEqual(asha['Confirmed Venue Booking Value'], 1220 + 1020 + 2020 + 900)
        self.assertEqual(asha['Paid Event Orders'], 5)
        self.assertEqual(asha['Paid Event Order Value'], 1270 * 4 + 520)
        self.assertEqual(asha['Refunded Bookings & Orders'], 3)

    def test_search_matches_the_page_fields(self):
        for q, expected in (('asha', {self.w.asha.id}), ('9100000002', {self.w.ravi.id}),
                            ('EXAMPLE.COM', {self.w.asha.id, self.w.ravi.id}),
                            (str(self.w.ravi.id), {self.w.ravi.id}), ('nobody', set())):
            ids = {r['User ID'] for r in self.rows('users', 'Users', q=q)}
            if q == str(self.w.ravi.id):
                self.assertIn(self.w.ravi.id, ids)
            else:
                self.assertEqual(ids, expected, q)

    def test_status_filter(self):
        blocked = self.rows('users', 'Users', status='blocked')
        self.assertEqual([r['User ID'] for r in blocked], [self.w.ravi.id])
        self.assertEqual(blocked[0]['Account Status'], 'Blocked')
        active = {r['User ID'] for r in self.rows('users', 'Users', status='active')}
        self.assertEqual(active, {self.w.asha.id})

    def test_empty_result_is_a_valid_workbook(self):
        response = self.export('users', q='no-such-user')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_sheets(response)['Users'], [])
        self.assertEqual(headers(response, 'Users')[0], 'User ID')

    def test_many_users(self):
        for i in range(30):
            make_customer(phone=f'92000000{i:02d}', name=f'Bulk {i}')
        self.assertEqual(len(self.rows('users', 'Users', q='bulk')), 30)


class VendorsExportTests(AdminExportTestCase):

    def test_rows_and_money(self):
        rows = {r['Vendor ID']: r for r in self.rows('vendors', 'Vendors')}
        self.assertEqual(set(rows), {self.w.vendor.id, self.w.other_vendor.id})
        one = rows[self.w.vendor.id]
        self.assertEqual(one['Phone'], '9000000001')
        self.assertEqual(one['KYC Status'], 'Pending')
        self.assertEqual(one['Account Status'], 'Active')
        self.assertEqual(one['Venues (excl. deleted)'], 3)     # unit + deleted folded out
        self.assertEqual(one['Live Venues'], 1)
        self.assertEqual(one['Venue Names'], 'City Turf, Grand Hall, New Banquet')
        self.assertEqual(one['Events Submitted'], 1)
        self.assertEqual(one['Kept Bookings'], 5)
        self.assertEqual(one['Gross Booking Value (kept)'], 1220 + 1020 + 500 + 2020 + 900)
        self.assertEqual(one['Platform Fees (kept)'], 80)
        # amount + platform promo top-up − fee, the dashboard's formula.
        self.assertEqual(one['Vendor Net Earnings'], 1200 + 1000 + 500 + 2000 + 980)
        self.assertEqual(one['Refund Pending Bookings'], 1)
        self.assertEqual(one['Refunded Bookings'], 2)
        self.assertEqual(one['Refunded Amount'], 300 + 720)
        self.assertEqual(rows[self.w.other_vendor.id]['Gross Booking Value (kept)'], 5000)

    def test_filters(self):
        self.w.other_vendor.is_active = False
        self.w.other_vendor.kyc = 'verified'
        self.w.other_vendor.save()
        self.assertEqual([r['Vendor ID'] for r in self.rows('vendors', 'Vendors', acc='suspended')],
                         [self.w.other_vendor.id])
        self.assertEqual([r['Vendor ID'] for r in self.rows('vendors', 'Vendors', kyc='verified')],
                         [self.w.other_vendor.id])
        self.assertEqual([r['Vendor ID'] for r in self.rows('vendors', 'Vendors', q='vendor one')],
                         [self.w.vendor.id])

    def test_no_credentials_or_bank_details(self):
        response = self.export('vendors')
        joined = ' '.join(headers(response, 'Vendors')).lower()
        for word in ('password', 'token', 'account number', 'ifsc', 'otp'):
            self.assertNotIn(word, joined)
        self.assertNotIn('pbkdf2', raw_xml(response))


class VenuesExportTests(AdminExportTestCase):

    def names(self, **params):
        return [r['Venue Name'] for r in self.rows('venues', 'Venues', **params)]

    def test_live_scope_matches_the_live_venues_page(self):
        self.assertEqual(sorted(self.names(scope='live')), ['Grand Hall', 'Rival Arena'])

    def test_all_scope_lists_every_venue_once(self):
        self.assertEqual(sorted(self.names()),
                         ['City Turf', 'Grand Hall', 'New Banquet', 'Old Place', 'Rival Arena'])

    def test_status_chip_filter(self):
        self.assertEqual(self.names(status='paused'), ['City Turf'])
        self.assertEqual(self.names(status='deleted'), ['Old Place'])
        self.assertEqual(self.names(status='draft'), ['New Banquet'])

    def test_unit_bookings_fold_into_their_venue(self):
        hall = next(r for r in self.rows('venues', 'Venues') if r['Venue Name'] == 'Grand Hall')
        self.assertEqual(hall['Unit Listings'], 1)
        self.assertEqual(hall['Kept Bookings (all units)'], 5)
        self.assertEqual(hall['Gross Booking Value (kept)'], 1220 + 1020 + 500 + 2020 + 900)
        self.assertEqual(hall['Listed Price (₹/hr)'], 1200)
        self.assertEqual(hall['Vendor'], 'Vendor One')
        self.assertEqual(hall['Panel Status'], 'Live')
        self.assertEqual(hall['Approval Status'], 'Approved')


class ApprovalsExportTests(AdminExportTestCase):

    def test_exports_every_page_of_matching_submissions(self):
        for i in range(12):
            self.w.listing(self.w.vendor, f'Queue Hall {i}', status=Listing.Status.PENDING,
                           city='Warangal', category='Resort')
        rows = self.rows('approvals', 'Approvals')
        self.assertEqual(len(rows), 13)           # the page shows 10 per page
        self.assertEqual(len(self.rows('approvals', 'Approvals', category='Resort')), 12)
        self.assertEqual(len(self.rows('approvals', 'Approvals', city='Hanamkonda')), 1)
        self.assertEqual(len(self.rows('approvals', 'Approvals', status='approved')), 0)

    def test_row(self):
        row = self.rows('approvals', 'Approvals')[0]
        self.assertEqual(row['Venue Name'], 'New Banquet')
        self.assertEqual(row['Approval Status'], 'Pending')
        self.assertEqual(row['Contact Phone'], '9000000001')
        self.assertEqual(row['Submitted'].date(),
                         Listing.objects.get(name='New Banquet').created_at.date())


class EventsExportTests(AdminExportTestCase):

    def test_tabs(self):
        names = lambda **p: sorted(r['Event Name'] for r in self.rows('events', 'Events', **p))
        self.assertEqual(names(), ['Garba Night', 'Hall Night', 'Rival Fest', 'Vendor One Fest'])
        self.assertEqual(names(tab='pending'), ['Vendor One Fest'])
        self.assertEqual(names(tab='live'), ['Garba Night', 'Hall Night', 'Rival Fest'])
        self.assertEqual(names(tab='promoted'), [])

    def test_sales_use_paid_orders_only(self):
        sheets = self.sheets('events')
        garba = next(r for r in sheets['Events'] if r['Event Name'] == 'Garba Night')
        self.assertEqual(garba['Paid Orders'], 2)
        self.assertEqual(garba['Tickets Sold'], 4)
        self.assertEqual(garba['Ticket Revenue (excl. fee)'], 1250 + 500)
        self.assertEqual(garba['Platform Fees on Paid Orders'], 40)
        self.assertEqual(garba['Refund Pending Orders'], 1)
        self.assertEqual(garba['Refund Pending Amount'], 1270)
        self.assertEqual(garba['Refunded Orders'], 1)
        self.assertEqual(garba['Refunded Amount'], 1270)   # full captured: no partial recorded
        self.assertEqual(garba['Total Capacity'], 200)
        self.assertEqual(garba['Seats Sold (paid)'], 3 + 1 + 3)
        self.assertEqual(garba['Managed By'], 'Admin')
        rival = next(r for r in sheets['Events'] if r['Event Name'] == 'Rival Fest')
        self.assertEqual(rival['Submitting Vendor'], 'Vendor Two')
        self.assertEqual(rival['Vendor Phone'], '9000000002')

        types = {(r['Event Name'], r['Key']): r for r in sheets['Ticket Types']}
        self.assertEqual(types[('Garba Night', 'adult')]['Tickets Sold (paid)'], 3)
        self.assertEqual(types[('Garba Night', 'adult')]['Ticket Revenue'], 1500)
        self.assertEqual(types[('Garba Night', 'child')]['Tickets Sold (paid)'], 1)

        dates = [r for r in sheets['Dates'] if r['Event Name'] == 'Garba Night']
        self.assertEqual(len(dates), 2)
        first = min(dates, key=lambda r: r['Date'])
        self.assertEqual(first['Seats Sold (paid)'], 7)
        self.assertEqual(first['Seats Held in Checkout'], 3)
        self.assertEqual(first['Remaining'], 90)


class PayoutsAndRefundsExportTests(AdminExportTestCase):

    def test_payout_tabs(self):
        pending = self.rows('payouts', 'Payouts', status='pending')
        self.assertEqual([(r['Vendor'], r['Payout Amount']) for r in pending], [('Vendor One', 4321)])
        self.assertEqual(len(self.rows('payouts', 'Payouts')), 2)
        self.assertEqual(self.rows('payouts', 'Payouts', status='failed'), [])

    def test_refund_tabs(self):
        requests = self.rows('refunds', 'Refunds', tab='requests')
        self.assertEqual([r['Booking ID'] for r in requests], [self.w.b_refund_pending.id])
        self.assertEqual(requests[0]['Captured Online'], 820)
        history = {r['Booking ID']: r for r in self.rows('refunds', 'Refunds', tab='history')}
        self.assertEqual(set(history), {self.w.b_refunded.id, self.w.b_refunded_full.id})
        self.assertEqual(history[self.w.b_refunded.id]['Refund Amount'], 300)
        self.assertEqual(history[self.w.b_refunded.id]['Refund Amount Basis'], 'Recorded refund amount')
        self.assertEqual(history[self.w.b_refunded_full.id]['Refund Amount'], 720)
        self.assertIn('Full captured amount', history[self.w.b_refunded_full.id]['Refund Amount Basis'])

    def test_paused_venue_tab(self):
        paid = self.w.booking(self.w.turf, 'confirmed', days=2, pay='pay_t', method='upi')
        self.w.booking(self.w.turf, 'confirmed', days=2, method='walk-in', walk_in=True, fee=0)
        self.w.booking(self.w.turf, 'confirmed', days=-2, pay='pay_t2')   # already hosted
        rows = self.rows('refunds', 'Refunds', tab='paused')
        self.assertEqual([r['Booking ID'] for r in rows], [paid.id])
        self.assertEqual(rows[0]['Refund Status'], 'Awaiting refund (venue paused)')


class PaymentsReportTests(AdminExportTestCase):

    def summary(self, **params):
        rows = self.rows('payments', 'Summary', **params)
        return {r['Metric']: r for r in rows}

    def test_summary_reconciles_with_the_payment_records(self):
        s = self.summary()
        def check(metric, venue, event):
            self.assertEqual(s[metric]['Venue Bookings'], venue, metric)
            self.assertEqual(s[metric]['Event Tickets'], event, metric)
            self.assertEqual(s[metric]['Total'], venue + event, metric)
        check('Records in period', 13, 11)
        check('Kept bookings', 6, 5)
        check('Gross booking value (kept)', 1220 + 1020 + 500 + 2020 + 900 + 5000, 1270 * 4 + 520)
        check('Platform fees (kept)', 100, 100)
        check('Platform-funded promo discounts (kept)', 100, 0)
        check('Collected at venue (kept)', 500 + 1500, 0)
        captured_v = 1220 + 1020 + 520 + 900 + 820 + 620 + 720 + 410 + 5000
        captured_e = 1270 * 7 + 520
        check('Captured online (payment gateway)', captured_v, captured_e)
        check('Refunds issued', 300 + 720, 1270)
        check('Net captured after refunds issued', captured_v - 1020, captured_e - 1270)
        check('Refunds pending', 820, 1270)
        check('Paid then cancelled — no refund recorded', 410, 1270)
        check('Unpaid / failed / abandoned attempts', 3, 3)
        self.assertEqual(s['Report period']['Notes'], 'All time')

    def test_detail_sheets(self):
        sheets = self.sheets('payments')
        venue = {r['Booking ID']: r for r in sheets['Venue Payments']}
        self.assertNotIn(self.w.b_removed.id, venue)
        self.assertEqual(len(venue), 13)
        part = venue[self.w.b_part.id]
        self.assertEqual(part['Captured Online'], 520)       # only the online share
        self.assertEqual(part['Collected at Venue'], 1500)
        self.assertEqual(part['Kept'], 'Yes')
        self.assertEqual(part['Razorpay Payment ID'], 'pay_3')
        self.assertEqual(venue[self.w.b_hold.id]['Captured Online'], 0)
        self.assertEqual(venue[self.w.b_hold.id]['Kept'], 'No')
        self.assertEqual(venue[self.w.b_promo.id]['Discount Funded By'], 'Platform')
        self.assertEqual(venue[self.w.b_cancelled_paid.id]['Refund Status'],
                         'Cancelled after payment — no refund recorded')
        events = {r['Order ID']: r for r in sheets['Event Payments']}
        self.assertEqual(len(events), 11)
        self.assertEqual(events[self.w.o_paid.id]['Tickets'], '2× Adult, 1× Child')
        self.assertEqual(events[self.w.o_paid.id]['Ticket Subtotal'], 1250)
        self.assertEqual({r['Booking ID'] for r in sheets['Refunds']}, {
            self.w.b_refund_pending.id, self.w.b_refunded.id, self.w.b_refunded_full.id,
            self.w.b_cancelled_paid.id, self.w.o_refund_pending.id, self.w.o_refunded.id,
            self.w.o_cancelled_paid.id,
        })

    def test_booking_ids_are_exported_exactly_as_stored(self):
        legacy = self.w.booking(self.w.hall, 'confirmed', days=1, pay='pay_legacy')
        Booking.objects.filter(pk=legacy.pk).update(id='bk_3f8a92c1d04e')
        ids = {r['Booking ID'] for r in self.rows('payments', 'Venue Payments')}
        self.assertIn('bk_3f8a92c1d04e', ids)
        self.assertIn(self.w.b_paid.id, ids)
        self.assertRegex(self.w.b_paid.id, r'^[A-Z0-9]{6}$')

    def test_date_range_reads_the_day_the_booking_was_made(self):
        ten_days_ago = timezone.now() - datetime.timedelta(days=10)
        Booking.objects.filter(pk=self.w.b_paid.pk).update(created_at=ten_days_ago)
        day = (self.w.today - datetime.timedelta(days=10)).isoformat()
        sheets = self.sheets('payments', **{'from': day, 'to': day})
        self.assertEqual([r['Booking ID'] for r in sheets['Venue Payments']], [self.w.b_paid.id])
        self.assertEqual(sheets['Event Payments'], [])
        summary = {r['Metric']: r for r in sheets['Summary']}
        self.assertEqual(summary['Gross booking value (kept)']['Total'], 1220)
        self.assertIn('day the booking was made', summary['Report period']['Notes'])
        # Everything else was made today.
        rest = self.sheets('payments', **{'from': self.w.today.isoformat()})
        self.assertNotIn(self.w.b_paid.id, {r['Booking ID'] for r in rest['Venue Payments']})


class AuditExportTests(AdminExportTestCase):

    def test_exports_the_whole_log_not_just_the_panel_100(self):
        self.w.audit(130)
        rows = self.rows('audit', 'Audit Log')
        self.assertGreaterEqual(len(rows), 130)
        self.assertEqual(set(rows[0]), {'When', 'Admin', 'Action', 'Target', 'Target ID', 'Change'})
