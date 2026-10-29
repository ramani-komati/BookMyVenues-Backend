"""
GET /api/vendors/me/exports/<kind> — the vendor dashboard's Excel exports.

  * vendor JWT only; customers, anonymous callers and the admin session are
    refused
  * ISOLATION: a vendor's export never contains another vendor's bookings,
    venues, events or ticket buyers, and another vendor's venue id cannot be
    used as a filter
  * the Bookings export lists exactly what the Bookings tab lists, with its
    filters; the Earnings report reconciles with the dashboard's total
  * refunded bookings appear in the Earnings report without the customer's
    phone number (the dashboard never showed it)
"""
import datetime
import uuid
from unittest.mock import patch

from rest_framework.test import APIClient

from bookings.models import Booking
from events.tests import EventTestCase, admin_client, auth

from .fixtures import World, headers, read_sheets

URL = '/api/vendors/me/exports/{}'


class VendorExportTestCase(EventTestCase):

    def setUp(self):
        super().setUp()
        self.w = World()
        self.client = auth(self.w.vendor)

    def export(self, kind, client=None, **params):
        return (client or self.client).get(URL.format(kind), params)

    def sheets(self, kind, client=None, **params):
        response = self.export(kind, client=client, **params)
        self.assertEqual(response.status_code, 200, getattr(response, 'data', None))
        return read_sheets(response)

    def ids(self, client=None, **params):
        return [r['Booking ID'] for r in self.sheets('bookings', client=client, **params)['Bookings']]


class AccessTests(VendorExportTestCase):

    KINDS = ('bookings', 'earnings', 'events', 'venues')

    def test_every_export_downloads_for_the_vendor(self):
        day = self.w.today.isoformat()
        names = {'bookings': 'vendor_bookings', 'earnings': 'vendor_earnings',
                 'events': 'vendor_event_sales', 'venues': 'vendor_venues'}
        for kind in self.KINDS:
            response = self.export(kind)
            self.assertEqual(response.status_code, 200, kind)
            self.assertTrue(response.content.startswith(b'PK'))
            self.assertEqual(response['Content-Disposition'],
                             f'attachment; filename="{names[kind]}_{day}.xlsx"')
            self.assertEqual(response['Cache-Control'], 'no-store')

    def test_non_vendors_are_refused(self):
        for client in (APIClient(), auth(self.w.asha), admin_client(self.w.admin)):
            for kind in self.KINDS:
                self.assertIn(self.export(kind, client=client).status_code, (401, 403), kind)

    def test_unknown_export_is_404(self):
        self.assertEqual(self.export('payouts').status_code, 404)

    def test_bad_filters_are_refused(self):
        for params in ({'status': 'refunded'}, {'from': '2026-13-01'},
                       {'from': '2026-10-10', 'to': '2026-10-01'}, {'venue': 'not-a-uuid'}):
            response = self.export('bookings', **params)
            self.assertEqual(response.status_code, 400, params)
            self.assertIn('message', response.json())

    def test_oversized_export_is_refused_not_truncated(self):
        with patch('reports.common.MAX_ROWS', 2):
            response = self.export('bookings')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Narrow the filters', response.json()['message'])

    def test_exports_change_nothing(self):
        before = sorted(Booking.objects.values_list('id', 'status', 'amount', 'collected'))
        for kind in self.KINDS:
            self.export(kind)
        self.assertEqual(before, sorted(Booking.objects.values_list('id', 'status', 'amount', 'collected')))


class IsolationTests(VendorExportTestCase):

    def test_bookings_hold_only_the_callers_own(self):
        mine = set(self.ids())
        theirs = set(self.ids(client=auth(self.w.other_vendor)))
        self.assertNotIn(self.w.b_other.id, mine)
        self.assertEqual(theirs, {self.w.b_other.id})
        self.assertFalse(mine & theirs)

    def test_another_vendors_venue_cannot_be_used_as_a_filter(self):
        foreign = self.export('bookings', venue=str(self.w.arena.id))
        missing = self.export('bookings', venue=str(uuid.uuid4()))
        self.assertEqual(foreign.status_code, 400)
        # Same answer as an id that does not exist — nothing to probe.
        self.assertEqual(foreign.json(), missing.json())
        self.assertEqual(self.export('earnings', venue=str(self.w.arena.id)).status_code, 400)

    def test_earnings_never_count_another_vendors_money(self):
        rows = self.sheets('earnings')['Bookings']
        self.assertNotIn(self.w.b_other.id, {r['Booking ID'] for r in rows})
        theirs = {r['Metric']: r['Value'] for r in
                  self.sheets('earnings', client=auth(self.w.other_vendor))['Summary']}
        self.assertEqual(theirs['Gross booking value'], 5000)

    def test_events_hold_only_visible_events_and_their_buyers(self):
        sheets = self.sheets('events')
        self.assertEqual(sorted(r['Event Name'] for r in sheets['Events']),
                         ['Hall Night', 'Vendor One Fest'])
        orders = {r['Order ID'] for r in sheets['Orders']}
        self.assertEqual(orders, {self.w.o_own.id, self.w.o_hall.id})
        self.assertNotIn('9100000099', {r['Customer Phone'] for r in sheets['Orders']})

    def test_venues_hold_only_the_callers_listings(self):
        names = sorted(r['Venue Name'] for r in self.sheets('venues')['Venues'])
        self.assertEqual(names, ['City Turf', 'Grand Hall', 'Grand Hall — Screen 2', 'New Banquet'])


class BookingsExportTests(VendorExportTestCase):

    def dashboard(self):
        response = self.client.get('/api/vendors/me/dashboard')
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_columns(self):
        self.assertEqual(headers(self.export('bookings'), 'Bookings')[:13], [
            'Booking ID', 'Venue', 'Pitch / Unit', 'Sport', 'Customer Name', 'Customer Phone',
            'Booking Date', 'Time Slots', 'Channel', 'Payment Method', 'Payment Status',
            'Booking Status', 'Booking Amount',
        ])

    def test_matches_the_bookings_tab_exactly(self):
        tab = [b['id'] for b in self.dashboard()['allBookings']]
        self.assertEqual(self.ids(), tab)   # same rows, same order (newest first)

    def test_status_filters_match_the_tab(self):
        by_status = {}
        for b in self.dashboard()['allBookings']:
            by_status.setdefault(b['status'], set()).add(b['id'])
        self.assertEqual(set(self.ids(status='upcoming')), by_status['confirmed'])
        self.assertEqual(set(self.ids(status='completed')), by_status['completed'])
        self.assertEqual(self.ids(status='refund_pending'), [self.w.b_refund_pending.id])

    def test_venue_filter_includes_its_unit_listings(self):
        self.assertEqual(set(self.ids(venue=str(self.w.hall.id))), {
            self.w.b_paid.id, self.w.b_done.id, self.w.b_walkin.id, self.w.b_part.id,
            self.w.b_promo.id,
        })
        self.assertEqual(self.ids(venue=str(self.w.turf.id)), [self.w.b_refund_pending.id])

    def test_date_range_and_search(self):
        day = (self.w.today + datetime.timedelta(days=2)).isoformat()
        self.assertEqual(self.ids(**{'from': day, 'to': day}), [self.w.b_paid.id])
        self.assertEqual(self.ids(q='walk-in'), [self.w.b_walkin.id])
        self.assertEqual(self.ids(q=self.w.b_part.id.lower()), [self.w.b_part.id])
        self.assertEqual(self.ids(q='no-such-booking'), [])

    def test_row_values(self):
        rows = {r['Booking ID']: r for r in self.sheets('bookings')['Bookings']}
        paid = rows[self.w.b_paid.id]
        self.assertEqual(paid['Venue'], 'Grand Hall')
        self.assertEqual(paid['Customer Name'], 'Asha')
        self.assertEqual(paid['Customer Phone'], '9100000001')
        self.assertEqual(paid['Booking Date'].date(), self.w.b_paid.date)
        self.assertEqual(paid['Channel'], 'Platform')
        self.assertEqual(paid['Payment Method'], 'UPI')
        self.assertEqual(paid['Payment Status'], 'Paid online')
        self.assertEqual(paid['Booking Status'], 'Upcoming')
        self.assertEqual(paid['Booking Amount'], 1220)
        self.assertEqual(paid['Platform Fee'], 20)
        self.assertEqual(paid['Your Earnings'], 1200)
        self.assertEqual(paid['Paid Online'], 1220)
        self.assertEqual(paid['Add-ons'], 'Cake')

        unit = rows[self.w.b_done.id]
        self.assertEqual((unit['Venue'], unit['Pitch / Unit']), ('Grand Hall', 'Screen 2'))
        self.assertEqual(unit['Booking Status'], 'Completed')

        part = rows[self.w.b_part.id]
        self.assertEqual(part['Payment Status'], 'Part-paid online')
        self.assertEqual((part['Paid Online'], part['Collect at Venue']), (520, 1500))
        self.assertEqual(part['Cash Collected'], 'No')

        walk_in = rows[self.w.b_walkin.id]
        self.assertEqual(walk_in['Channel'], 'Walk-in')
        self.assertEqual((walk_in['Platform Fee'], walk_in['Your Earnings']), (0, 500))
        self.assertIsNone(walk_in['Customer Phone'])

        promo = rows[self.w.b_promo.id]
        self.assertEqual(promo['Your Earnings'], 900 + 100 - 20)
        self.assertEqual(promo['Discount Funded By'], 'Platform')

        pending = rows[self.w.b_refund_pending.id]
        self.assertEqual(pending['Refund Status'], 'Refund pending')
        self.assertEqual(pending['Your Earnings'], 0)       # not the vendor's money


class EarningsReportTests(VendorExportTestCase):

    def summary(self, client=None, **params):
        return {r['Metric']: r['Value'] for r in self.sheets('earnings', client=client, **params)['Summary']}

    def test_net_earnings_equal_the_dashboard_total(self):
        dashboard = self.client.get('/api/vendors/me/dashboard').json()
        self.assertEqual(self.summary()['Net earnings'], dashboard['stats']['total']['value'])

    def test_gross_captured_refunds_and_net_are_kept_apart(self):
        s = self.summary()
        self.assertEqual(s['Kept bookings'], 5)
        self.assertEqual(s['Gross booking value'], 1220 + 1020 + 500 + 2020 + 900)
        self.assertEqual(s['Less: platform fees'], 80)
        self.assertEqual(s['Plus: platform promo top-ups'], 100)
        self.assertEqual(s['Net earnings'], 5660 - 80 + 100)
        self.assertEqual(s['   from walk-ins'], 500)
        self.assertEqual(s['   from platform bookings'], 5680 - 500)
        self.assertEqual(s['Paid online'], 1220 + 1020 + 520 + 900)
        self.assertEqual(s['Collected at venue'], 500 + 1500)
        self.assertEqual(s['Refund pending — bookings'], 1)
        self.assertEqual(s['Refund pending — amount'], 820)
        self.assertEqual(s['Refunded — bookings'], 2)
        self.assertEqual(s['Refunded — amount'], 300 + 720)
        self.assertEqual(s['Report period'], 'All time')

    def test_breakdowns(self):
        sheets = self.sheets('earnings')
        venues = {r['Venue']: r for r in sheets['By Venue']}
        self.assertEqual(set(venues), {'Grand Hall', 'City Turf'})
        self.assertEqual(venues['Grand Hall']['Net Earnings'], 5680)   # unit folded in
        self.assertEqual(venues['City Turf']['Refunded Amount'], 300)
        self.assertEqual(sum(r['Gross Booking Value'] for r in sheets['By Month']), 5660)

    def test_refunded_rows_carry_no_customer_phone(self):
        rows = {r['Booking ID']: r for r in self.sheets('earnings')['Bookings']}
        self.assertIsNone(rows[self.w.b_refunded.id]['Customer Phone'])
        self.assertEqual(rows[self.w.b_refunded.id]['Refund Amount'], 300)
        self.assertEqual(rows[self.w.b_refund_pending.id]['Customer Phone'], '9100000001')
        # Unpaid, failed and cancelled attempts are never in an earnings report.
        for booking in (self.w.b_hold, self.w.b_cancelled, self.w.b_failed, self.w.b_removed):
            self.assertNotIn(booking.id, rows)

    def test_filters(self):
        turf = self.summary(venue=str(self.w.turf.id))
        self.assertEqual(turf['Gross booking value'], 0)
        self.assertEqual(turf['Refunded — amount'], 300)
        self.assertEqual(turf['Venues'], 'City Turf')
        future = (self.w.today + datetime.timedelta(days=1)).isoformat()
        s = self.summary(**{'from': future})
        self.assertEqual(s['Gross booking value'], 1220 + 500 + 2020 + 900)


class EventsExportTests(VendorExportTestCase):

    def test_sales_sheets(self):
        sheets = self.sheets('events')
        events = {r['Event Name']: r for r in sheets['Events']}
        own = events['Vendor One Fest']
        self.assertEqual(own['Approval Status'], 'Pending review')
        self.assertEqual(own['Submitted By'], 'You')
        self.assertEqual(own['Paid Orders'], 1)
        self.assertEqual(own['Ticket Revenue (excl. fee)'], 1250)
        self.assertEqual(events['Hall Night']['Submitted By'], 'TheBookMyVenues (at your venue)')
        self.assertNotIn('Vendor Phone', events['Hall Night'])        # admin-only column
        self.assertEqual({r['Event Name'] for r in sheets['Ticket Types']},
                         {'Vendor One Fest', 'Hall Night'})
        hall_date = next(r for r in sheets['Dates'] if r['Event Name'] == 'Hall Night')
        self.assertEqual(hall_date['Seats Sold (paid)'], 3)
        self.assertEqual(hall_date['Seats Held in Checkout'], 3)
        order = next(r for r in sheets['Orders'] if r['Order ID'] == self.w.o_own.id)
        self.assertEqual(order['Payment Status'], 'Paid')
        self.assertEqual(order['Order Total'], 1270)
        self.assertEqual(order['Customer Phone'], '9100000001')
