"""
Live venues / All venues search, and the Users page email column — the
server half.

  * the venues export applies the page's search (?q=) over the same fields
    the panel searches (SuperAdmin features/venues/venueSearch.js): venue
    name, vendor contact or business name, category and its group label,
    city / district / area, venue id — together with scope and status
  * ?pin=unpinned exports only the venues without a valid map location
  * admin venue rows carry the vendor's business name (never bank details)
  * the users export has the Email column; a customer without an email
    gets a blank cell, and the bootstrap row sends '' — never None
"""
from accounts.models import User, VendorSettings
from adminpanel.formatters import BootstrapContext, format_venues, user_row
from events.tests import make_customer
from venues.models import Listing, PayoutDetails

from .fixtures import raw_xml
from .test_admin_reports import AdminExportTestCase


class VenueSearchExportTests(AdminExportTestCase):

    def names(self, **params):
        return sorted(r['Venue Name'] for r in self.rows('venues', 'Venues', **params))

    def test_search_by_venue_name_and_vendor_name(self):
        self.assertEqual(self.names(q='grand'), ['Grand Hall'])
        self.assertEqual(self.names(q='GRAND'), ['Grand Hall'])
        self.assertEqual(self.names(q='vendor two'), ['Rival Arena'])

    def test_search_by_business_name(self):
        VendorSettings.objects.create(user=self.w.other_vendor, business_name='Arena Sports LLP')
        self.assertEqual(self.names(q='arena sports'), ['Rival Arena'])

    def test_search_by_city_and_id(self):
        self.assertEqual(self.names(q='warangal'), ['Grand Hall'])
        self.assertEqual(self.names(q=str(self.w.arena.pk)[:8]), ['Rival Arena'])

    def test_search_by_category_and_its_group(self):
        Listing.objects.filter(pk=self.w.arena.pk).update(category='Play zone')
        self.assertEqual(self.names(q='play zone'), ['Rival Arena'])
        self.assertEqual(self.names(q='sports'), ['Rival Arena'])

    def test_search_combines_with_scope_and_status(self):
        self.assertEqual(self.names(scope='live', q='vendor one'), ['Grand Hall'])
        self.assertEqual(self.names(status='paused', q='vendor one'), ['City Turf'])
        self.assertEqual(self.names(status='paused', q='rival'), [])
        # Unit siblings stay folded into their venue, as on the page.
        self.assertEqual(self.names(q='screen 2'), [])

    def test_no_match_is_an_empty_sheet_not_an_error(self):
        self.assertEqual(self.names(q='nowhere at all'), [])

    def test_pin_filter(self):
        Listing.objects.filter(pk=self.w.arena.pk).update(latitude=17.9689, longitude=79.5941)
        self.assertEqual(self.names(scope='live', pin='unpinned'), ['Grand Hall'])
        self.assertEqual(self.names(scope='live', pin='All'), ['Grand Hall', 'Rival Arena'])
        self.assertEqual(self.export('venues', pin='bogus').status_code, 400)

    def test_overlong_search_is_refused(self):
        self.assertEqual(self.export('venues', q='x' * 101).status_code, 400)

    def test_searching_never_reveals_bank_details(self):
        PayoutDetails.objects.create(
            user=self.w.vendor, account_holder='Vendor One', bank_name='HDFC',
            account_number='123456789012', ifsc='HDFC0001234', payout_phone='9000000001',
        )
        self.assertEqual(self.names(q='hdfc0001234'), [])
        self.assertEqual(self.names(q='123456789012'), [])
        response = self.export('venues', q='grand')
        self.assertNotIn('123456789012', raw_xml(response))
        self.assertNotIn('HDFC0001234', raw_xml(response))


class VenueRowBusinessNameTests(AdminExportTestCase):

    def test_rows_carry_the_business_name_or_blank(self):
        VendorSettings.objects.create(user=self.w.vendor, business_name='One Events')
        rows = {r['name']: r for r in format_venues(BootstrapContext())}
        self.assertEqual(rows['Grand Hall']['vendorBusiness'], 'One Events')
        self.assertEqual(rows['Grand Hall']['vendor'], 'Vendor One')
        self.assertEqual(rows['Rival Arena']['vendorBusiness'], '')

    def test_bootstrap_rows_carry_it(self):
        VendorSettings.objects.create(user=self.w.vendor, business_name='One Events')
        venues = self.client.get('/api/admin/bootstrap').json()['venues']
        hall = next(v for v in venues if v['name'] == 'Grand Hall')
        self.assertEqual(hall['vendorBusiness'], 'One Events')


class UserEmailTests(AdminExportTestCase):

    def setUp(self):
        super().setUp()
        self.no_email = make_customer('9100000077', 'Meena')
        User.objects.filter(pk=self.no_email.pk).update(email=None)

    def test_bootstrap_rows_send_the_email_or_an_empty_string(self):
        users = {u['name']: u for u in self.client.get('/api/admin/bootstrap').json()['users']}
        self.assertEqual(users['Asha']['email'], self.w.asha.email)
        self.assertEqual(users['Meena']['email'], '')
        for row in users.values():
            self.assertIsInstance(row['email'], str)
            for secret in ('password', 'otp', 'token', 'code_hash'):
                self.assertNotIn(secret, {k.lower() for k in row})

    def test_user_row_blank_email(self):
        self.no_email.refresh_from_db()
        self.assertEqual(user_row(self.no_email)['email'], '')

    def test_users_export_has_the_email_column_and_blank_cells(self):
        rows = {r['Name']: r for r in self.rows('users', 'Users')}
        self.assertEqual(rows['Asha']['Email'], self.w.asha.email)
        self.assertIn(rows['Meena']['Email'], (None, ''))
        self.assertNotIn('None', [str(rows['Meena']['Email'] or '')])

    def test_users_export_search_reads_email(self):
        rows = self.rows('users', 'Users', q=self.w.asha.email.upper())
        self.assertEqual([r['Name'] for r in rows], ['Asha'])
