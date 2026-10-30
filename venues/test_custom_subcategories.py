"""
Custom sub-categories (venues/subcategories.py) — the registration wizards'
"Others" option.

  * a vendor's draft submit records its typed names as PENDING under the
    draft's own category; the super admin's Register Venues flow (a vendor
    token with an admin `actor`) records them APPROVED
  * names are tidied and de-duplicated (case, spaces, punctuation); a name
    that is really a built-in one is never stored; empty / junk names and
    categories without a sub-category list are refused
  * a rejected name cannot be re-submitted by a vendor
  * the admin endpoints list, add (approved at once, duplicates refused) and
    approve / reject — every write audited
  * customers and other vendors only ever see APPROVED names: /api/config,
    /api/subcategories, the venue list rows and the venue page
  * existing venues and the built-in lists are untouched
"""
import copy
import uuid

from django.core.cache import cache
from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from adminpanel.models import AuditEntry

from .models import CustomSubCategory, Listing, VenueDraft
from .subcategories import SubCategoryError, clean_name, ensure, name_key
from .tests import FULL_DATA


def sports_draft_data(*custom, extra_sports=()):
    data = copy.deepcopy(FULL_DATA)
    sports = {name: True for name in (*extra_sports, *custom)}
    data['details'] = {
        'primaryCategory': 'Playzone',
        'categoryGroup': 'sports',
        'capacity': '20',
        'amenities': ['WiFi', 'Parking'],
        'sports': sports,
        'sportConfig': {name: {'price': '800', 'units': '1'} for name in sports},
        'customSubCategories': list(custom),
    }
    return data


class SubCategoryRulesTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_names_are_tidied(self):
        self.assertEqual(clean_name('   Cricket    Nets  '), 'Cricket Nets')
        self.assertEqual(name_key('cricket-NETS'), name_key('  Cricket  Nets '))

    def test_empty_and_junk_names_are_refused(self):
        for bad in ('', '   ', 'x', '1234', 'Others', 'other', 'a' * 41, 'Nets · Pitch', 'Hall — 2', '<b>x</b>'):
            with self.assertRaises(SubCategoryError, msg=bad):
                clean_name(bad)

    def test_a_built_in_name_is_never_stored(self):
        row, created = ensure('Play zone', '  badminton ')
        self.assertIsNone(row)
        self.assertFalse(created)
        row, _ = ensure('Play zone', 'football')            # alias of Football Turf
        self.assertIsNone(row)
        self.assertEqual(CustomSubCategory.objects.count(), 0)

    def test_a_prefix_of_a_built_in_is_its_own_name(self):
        row, created = ensure('Play zone', 'Cricket')         # not "Box Cricket"
        self.assertTrue(created)
        self.assertEqual(row.name, 'Cricket')

    def test_duplicates_with_other_spacing_or_case_are_one_row(self):
        first, created = ensure('Play zone', 'Cricket Nets')
        again, created_again = ensure('Play zone', '  cricket   NETS ')
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(again.name, 'Cricket Nets')          # first spelling kept
        self.assertEqual(CustomSubCategory.objects.count(), 1)

    def test_the_same_name_under_another_category_is_separate(self):
        ensure('Play zone', 'Kids Zone')
        ensure('Party hall', 'Kids Zone')
        self.assertEqual(CustomSubCategory.objects.count(), 2)

    def test_categories_without_a_list_are_refused(self):
        for category in ('Private theatre', 'Resort', 'Rooftop Lounge', ''):
            with self.assertRaises(SubCategoryError, msg=category):
                ensure(category, 'Cricket Nets')


class _Base(APITestCase):
    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(
            phone='9000000001', name='Vendor One', email='v1@example.com', role=User.Role.VENDOR,
        )
        self.other_vendor = User.objects.create_user(
            phone='9000000002', name='Vendor Two', email='v2@example.com', role=User.Role.VENDOR,
        )
        self.admin = User.objects.create_user(
            phone='9000000009', name='Anita', email='admin@example.com',
            role=User.Role.ADMIN, is_staff=True, password='pw',
        )
        self.client.force_authenticate(user=self.vendor)
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)

    def submit(self, draft, client=None):
        return (client or self.client).post(f'/api/venues/drafts/{draft.pk}/submit', format='json')

    def admin_vendor_client(self, phone='9000000002'):
        """The super admin's Register Venues flow: a vendor token minted by
        POST /api/admin/vendors/token (it carries the admin `actor`)."""
        token = self.admin_client.post(
            '/api/admin/vendors/token', {'phone': phone, 'name': 'Owner'}, format='json',
        ).data['token']
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        return client

    def publish(self, draft, client=None, **detail):
        record = {
            'id': str(draft.pk), 'name': 'Arena', 'category': 'Play zone', 'price': '800',
            'detail': {
                'primaryCategory': 'Playzone',
                'sports': [{'name': n, 'price': '800', 'units': 1} for n in detail.pop('sports', [])],
                **detail,
            },
        }
        return (client or self.client).post('/api/vendors/me/listings', record, format='json')


class VendorRegistrationTests(_Base):
    def test_an_existing_sub_category_registers_as_before(self):
        draft = VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data(extra_sports=('Badminton',)))
        response = self.submit(draft)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['status'], 'pending')
        self.assertEqual(CustomSubCategory.objects.count(), 0)

    def test_sports_others_cricket_nets_is_saved_pending_under_sports(self):
        draft = VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data('Cricket Nets'))
        response = self.submit(draft)
        self.assertEqual(response.status_code, 200, response.data)
        row = CustomSubCategory.objects.get()
        self.assertEqual((row.category, row.name, row.status), ('Play zone', 'Cricket Nets', 'pending'))
        self.assertEqual(row.created_by, self.vendor)
        self.assertFalse(row.created_by_admin)

    def test_an_empty_custom_name_blocks_the_submit(self):
        data = sports_draft_data(extra_sports=('Badminton',))
        data['details']['customSubCategories'] = ['   ']
        draft = VenueDraft.objects.create(vendor=self.vendor, data=data)
        # declared_custom drops blank strings, so nothing is stored…
        self.assertEqual(self.submit(draft).status_code, 200)
        self.assertEqual(CustomSubCategory.objects.count(), 0)
        # …and a name that is present but unusable is refused outright.
        data['details']['customSubCategories'] = ['x']
        draft = VenueDraft.objects.create(vendor=self.vendor, data=data)
        response = self.submit(draft)
        self.assertEqual(response.status_code, 400)
        self.assertIn('2 characters', response.data['message'])
        self.assertEqual(VenueDraft.objects.get(pk=draft.pk).status, VenueDraft.Status.DRAFT)

    def test_two_vendors_typing_the_same_name_share_one_row(self):
        self.submit(VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data('Cricket Nets')))
        other = APIClient()
        other.force_authenticate(user=self.other_vendor)
        draft = VenueDraft.objects.create(vendor=self.other_vendor, data=sports_draft_data('cricket  nets'))
        self.assertEqual(self.submit(draft, other).status_code, 200)
        self.assertEqual(CustomSubCategory.objects.count(), 1)

    def test_a_rejected_name_cannot_be_resubmitted(self):
        CustomSubCategory.objects.create(
            category='Play zone', name='Cricket Nets', name_key=name_key('Cricket Nets'), status='rejected',
        )
        draft = VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data('Cricket Nets'))
        response = self.submit(draft)
        self.assertEqual(response.status_code, 400)
        self.assertIn('was not accepted', response.data['message'])

    def test_custom_names_only_under_categories_with_a_list(self):
        data = copy.deepcopy(FULL_DATA)
        data['details'] = {**data['details'], 'primaryCategory': 'Resort', 'customSubCategories': ['Treehouse']}
        response = self.submit(VenueDraft.objects.create(vendor=self.vendor, data=data))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(CustomSubCategory.objects.count(), 0)

    def test_publish_records_names_the_submit_never_saw(self):
        draft = VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data())
        response = self.publish(draft, sports=['Cricket Nets'], customSubCategories=[' cricket nets '])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(CustomSubCategory.objects.get().status, 'pending')
        stored = Listing.objects.get(pk=draft.pk).record['detail']
        self.assertEqual(stored['customSubCategories'], ['cricket nets'])


class SuperAdminRegistrationTests(_Base):
    def test_super_admin_registration_approves_its_names(self):
        client = self.admin_vendor_client()
        owner = User.objects.get(phone='9000000002')
        draft = VenueDraft.objects.create(vendor=owner, data=sports_draft_data('Archery Range'))
        self.assertEqual(self.submit(draft, client).status_code, 200)
        row = CustomSubCategory.objects.get()
        self.assertEqual(row.status, 'approved')
        self.assertTrue(row.created_by_admin)
        self.assertEqual(row.reviewed_by, 'Anita')

    def test_super_admin_using_a_vendors_pending_name_approves_it(self):
        self.submit(VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data('Cricket Nets')))
        self.assertEqual(CustomSubCategory.objects.get().status, 'pending')
        # The admin sees it…
        listed = self.admin_client.get('/api/admin/subcategories').data['subCategories']
        self.assertEqual([(r['name'], r['status']) for r in listed], [('Cricket Nets', 'pending')])
        self.assertEqual(listed[0]['createdBy']['phone'], '9000000001')
        # …and registers another venue with it.
        client = self.admin_vendor_client()
        owner = User.objects.get(phone='9000000002')
        draft = VenueDraft.objects.create(vendor=owner, data=sports_draft_data('Cricket Nets'))
        self.assertEqual(self.submit(draft, client).status_code, 200)
        self.assertEqual(CustomSubCategory.objects.get().status, 'approved')

    def test_a_plain_vendor_token_is_not_an_admin(self):
        # A vendor cannot fake the actor claim: only tokens the admin
        # endpoint minted for a real, active admin count.
        from rest_framework_simplejwt.tokens import RefreshToken
        token = RefreshToken.for_user(self.vendor).access_token
        token['actor'] = f'admin:{self.vendor.pk}'
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        draft = VenueDraft.objects.create(vendor=self.vendor, data=sports_draft_data('Cricket Nets'))
        self.assertEqual(self.submit(draft, client).status_code, 200)
        self.assertEqual(CustomSubCategory.objects.get().status, 'pending')


class AdminEndpointTests(_Base):
    def test_admin_adds_an_approved_name_vendors_then_see(self):
        response = self.admin_client.post(
            '/api/admin/subcategories', {'category': 'Playzone', 'name': '  Archery   Range '}, format='json',
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['subCategory']['status'], 'approved')
        self.assertEqual(response.data['subCategory']['category'], 'Play zone')
        self.assertTrue(AuditEntry.objects.filter(action='Added sub-category').exists())
        # The vendor wizard's list.
        self.assertEqual(self.client.get('/api/subcategories').data['categories'], {'Play zone': ['Archery Range']})

    def test_admin_duplicates_and_built_ins_are_refused(self):
        self.admin_client.post('/api/admin/subcategories', {'category': 'Play zone', 'name': 'Archery'}, format='json')
        dup = self.admin_client.post('/api/admin/subcategories', {'category': 'Play zone', 'name': 'ARCHERY '},
                                     format='json')
        self.assertEqual((dup.status_code, dup.data['code']), (409, 'DUPLICATE'))
        builtin = self.admin_client.post('/api/admin/subcategories', {'category': 'Play zone', 'name': 'badminton'},
                                         format='json')
        self.assertEqual((builtin.status_code, builtin.data['code']), (409, 'BUILT_IN'))
        empty = self.admin_client.post('/api/admin/subcategories', {'category': 'Play zone', 'name': '  '},
                                       format='json')
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(CustomSubCategory.objects.count(), 1)

    def test_admin_adding_a_pending_name_approves_it(self):
        ensure('Play zone', 'Cricket Nets', user=self.vendor)
        response = self.admin_client.post(
            '/api/admin/subcategories', {'category': 'Play zone', 'name': 'cricket nets'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(CustomSubCategory.objects.get().status, 'approved')

    def test_approve_and_reject(self):
        row, _ = ensure('Play zone', 'Cricket Nets', user=self.vendor)
        url = f'/api/admin/subcategories/{row.pk}'
        self.assertEqual(self.admin_client.patch(url, {'status': 'approved'}, format='json').status_code, 200)
        self.assertEqual(self.client.get('/api/subcategories').data['categories'], {'Play zone': ['Cricket Nets']})
        self.admin_client.patch(url, {'status': 'rejected', 'reason': 'not a sport'}, format='json')
        self.assertEqual(self.client.get('/api/subcategories').data['categories'], {})
        self.assertTrue(AuditEntry.objects.filter(action='Updated sub-category', change__contains='not a sport').exists())
        self.assertEqual(self.admin_client.patch(url, {'status': 'nope'}, format='json').status_code, 400)

    def test_admin_endpoints_are_admin_only(self):
        self.assertIn(self.client.get('/api/admin/subcategories').status_code, (401, 403))
        self.assertIn(
            self.client.post('/api/admin/subcategories', {'category': 'Play zone', 'name': 'X Y'}, format='json')
            .status_code, (401, 403),
        )
        self.assertEqual(CustomSubCategory.objects.count(), 0)


class CustomerVisibilityTests(_Base):
    def make_live(self, *custom, occasions=()):
        listing_id = uuid.uuid4()
        return Listing.objects.create(
            id=listing_id, vendor=self.vendor, slug=f'arena-{listing_id.hex[:6]}', name='Arena',
            category='Play zone', status=Listing.Status.LIVE,
            record={
                'id': str(listing_id), 'name': 'Arena', 'category': 'Play zone', 'price': '800',
                'detail': {
                    'primaryCategory': 'Playzone',
                    'sports': [{'name': n, 'price': '800', 'units': 1} for n in ('Badminton', *custom)],
                    'occasions': list(occasions),
                    'customSubCategories': list(custom),
                },
            },
        )

    def test_pending_names_never_reach_customers(self):
        ensure('Play zone', 'Cricket Nets', user=self.vendor)
        listing = self.make_live('Cricket Nets', occasions=['Cricket Nets'])
        public = APIClient()
        config = public.get('/api/config').data['categories']
        self.assertNotIn('Cricket Nets', next(c for c in config if c['name'] == 'Play zone')['subCategories'])
        self.assertEqual(public.get('/api/subcategories').data['categories'], {})
        detail = public.get(f'/api/venues/{listing.pk}').data
        self.assertEqual(detail['subCategories'], ['Badminton'])
        self.assertEqual(detail['detail']['occasions'], [])
        self.assertEqual(detail['detail']['customSubCategories'], [])
        # The pitch itself stays bookable.
        self.assertEqual([s['name'] for s in detail['detail']['sports']], ['Badminton', 'Cricket Nets'])
        rows = public.get('/api/venues').data
        rows = rows.get('venues', rows.get('results', rows)) if isinstance(rows, dict) else rows
        self.assertEqual(next(r for r in rows if r['id'] == str(listing.pk))['subCategories'], ['Badminton'])

    def test_approved_names_reach_customers(self):
        row, _ = ensure('Play zone', 'Cricket Nets', user=self.vendor)
        row.status = 'approved'
        row.save()
        cache.clear()
        listing = self.make_live('cricket nets')
        public = APIClient()
        config = public.get('/api/config').data['categories']
        self.assertIn('Cricket Nets', next(c for c in config if c['name'] == 'Play zone')['subCategories'])
        detail = public.get(f'/api/venues/{listing.pk}').data
        self.assertEqual(detail['subCategories'], ['Badminton', 'Cricket Nets'])
        self.assertEqual(detail['detail']['customSubCategories'], ['Cricket Nets'])

    def test_existing_venues_keep_their_sub_categories(self):
        listing = self.make_live()
        detail = APIClient().get(f'/api/venues/{listing.pk}').data
        self.assertEqual(detail['subCategories'], ['Badminton'])
        config = APIClient().get('/api/config').data['categories']
        self.assertIn('Box Cricket', next(c for c in config if c['name'] == 'Play zone')['subCategories'])
