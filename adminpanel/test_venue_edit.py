"""
All venues → Edit (adminpanel/venue_edit.py) — regression tests.

  * super admin only (vendors, customers and anonymous callers refused)
  * GET returns the draft WITHOUT the payout section
  * bank / payout keys are refused anywhere in the request, including a
    crafted nested key — and the stored bank details survive every edit
  * fields not sent keep their values (merge, not replace)
  * server-owned fields (status, owner, submittedAt, pause, unit links)
    cannot be changed through the edit
  * the vendor's draft is updated too, so the vendor portal's republish
    (self-heal) carries the admin's change instead of reverting it
  * pitch / screen listings follow the base; changing how many there are
    is refused
  * a stale version is refused instead of overwriting a newer save
  * every save is audited; deleted venues and unit listings are refused
"""
import copy
import uuid

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from venues.models import Listing, VenueDraft

from .models import AuditEntry

PAYOUT = {'acctHolder': 'Ravi', 'bankName': 'HDFC', 'acctNumber': '123456789012',
          'ifsc': 'HDFC0001234', 'payoutPhone': '9000000001'}

DRAFT = {
    'basics': {'venueName': 'Grand Hall', 'shortDescription': 'Old text', 'phone': '9000000001', 'email': 'v@x.in'},
    'location': {'houseStreet': '1 Main Rd', 'area': 'Hanamkonda', 'district': 'Warangal', 'pincode': '506001',
                 'city': 'Warangal', 'stateName': 'Telangana', 'mapsLink': ''},
    'details': {'primaryCategory': 'Private Hall', 'numScreens': '1', 'amenities': ['AC'],
                'openTime': '09:00', 'closeTime': '22:00', 'open24Hours': False,
                'paused': False, 'screenConfig': {'0': {'max': '50', 'price': '1500'}}},
    'payout': PAYOUT,
    'photos': {'venuePhotos': [{'id': 'p1', 'url': 'https://cdn.example/1.jpg'}], 'serviceImages': []},
}

RECORD = {
    'name': 'Grand Hall', 'category': 'Party hall', 'locality': 'Hanamkonda', 'city': 'Warangal',
    'district': 'Warangal', 'location': 'Hanamkonda, Warangal', 'price': '1500', 'unit': '/ slot',
    'image': 'https://cdn.example/1.jpg', 'gallery': ['https://cdn.example/1.jpg'],
    'submittedAt': '2026-10-01T10:00:00+00:00', 'status': 'live', 'meta': 'New', 'paused': False,
    'detail': {'description': 'Old text', 'capacity': '50', 'unitCount': 1, 'amenities': ['AC'],
               'parking': False, 'timings': {'open': '09:00', 'close': '22:00', 'open24Hours': False}},
}


class VenueEditTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(phone='9990000201', name='Anita', email='an@x.in',
                                              role=User.Role.ADMIN, password='pw')
        self.vendor = User.objects.create_user(phone='9000000001', name='Ravi', email='v@x.in',
                                               role=User.Role.VENDOR)
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall', record=copy.deepcopy(RECORD),
            name='Grand Hall', category='Party hall', locality='Hanamkonda', pincode='506001', status='live',
        )
        self.draft = VenueDraft.objects.create(id=self.listing.pk, vendor=self.vendor,
                                               data=copy.deepcopy(DRAFT), status='pending')
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)
        self.url = f'/api/admin/venues/{self.listing.pk}/edit'

    def load(self):
        response = self.admin_client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def body(self, sections=None, record=None, version=None):
        loaded = self.load()
        return {
            'version': loaded['version'] if version is None else version,
            'sections': sections if sections is not None else {
                'basics': {**DRAFT['basics'], 'venueName': 'Grand Palace', 'shortDescription': 'New text'},
                'details': {**DRAFT['details'], 'openTime': '10:00', 'closeTime': '23:00'},
            },
            'record': record if record is not None else {
                **RECORD, 'name': 'Grand Palace', 'status': 'pending', 'submittedAt': 'now',
                'paused': True, 'detail': {**RECORD['detail'], 'description': 'New text',
                                           'timings': {'open': '10:00', 'close': '23:00', 'open24Hours': False}},
            },
        }

    def put(self, body, client=None):
        return (client or self.admin_client).put(self.url, body, format='json')

    # -- access ---------------------------------------------------------

    def test_only_a_super_admin_may_edit(self):
        body = self.body()
        for user in (None, self.vendor, User.objects.create_user(phone='9000000003', name='C', email='c@x.in')):
            client = APIClient()
            if user is not None:
                client.force_login(user)
            self.assertIn(client.get(self.url).status_code, (401, 403))
            self.assertIn(self.put(body, client).status_code, (401, 403))
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).name, 'Grand Hall')

    def test_get_never_returns_bank_details(self):
        data = self.load()
        self.assertNotIn('payout', data['draft'])
        self.assertNotIn('123456789012', str(data))
        self.assertFalse(data['bankDetailsEditable'])

    # -- bank details -----------------------------------------------------

    def test_bank_details_are_refused_anywhere(self):
        attempts = [
            {'payout': {'acctNumber': '999999999999'}},
            {'basics': {**DRAFT['basics'], 'accountNumber': '999999999999'}},
            {'details': {**DRAFT['details'], 'extra': {'ifsc': 'SBIN0000001'}}},   # nested, crafted
        ]
        for sections in attempts:
            response = self.put(self.body(sections=sections))
            self.assertEqual(response.status_code, 400, sections)
            self.assertEqual(response.json()['code'], 'BANK_DETAILS_READ_ONLY')
        record = {**RECORD, 'detail': {**RECORD['detail'], 'bankDetails': {'ifsc': 'X'}}}
        self.assertEqual(self.put(self.body(record=record)).json()['code'], 'BANK_DETAILS_READ_ONLY')
        self.assertEqual(VenueDraft.objects.get(pk=self.draft.pk).data['payout'], PAYOUT)

    # -- saving -------------------------------------------------------------

    def test_edit_saves_listing_and_draft_and_keeps_bank_details(self):
        response = self.put(self.body())
        self.assertEqual(response.status_code, 200, response.content)
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual(listing.name, 'Grand Palace')                      # column
        self.assertEqual(listing.record['detail']['description'], 'New text')
        self.assertEqual(listing.record['detail']['timings']['open'], '10:00')
        draft = VenueDraft.objects.get(pk=self.draft.pk).data
        self.assertEqual(draft['basics']['venueName'], 'Grand Palace')       # vendor republish keeps it
        self.assertEqual(draft['details']['openTime'], '10:00')
        self.assertEqual(draft['payout'], PAYOUT)                            # untouched
        self.assertEqual(draft['location'], DRAFT['location'])               # not sent → unchanged

    def test_server_owned_fields_cannot_change(self):
        self.put(self.body())
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual(listing.status, 'live')
        self.assertEqual(listing.vendor_id, self.vendor.id)
        self.assertEqual(listing.record['status'], 'live')
        self.assertEqual(listing.record['submittedAt'], RECORD['submittedAt'])
        self.assertFalse(listing.record['paused'])                          # vendor's pause

    def test_fields_not_sent_keep_their_values(self):
        record = {'name': 'Grand Hall', 'category': 'Party hall', 'detail': {'description': 'Only this'}}
        sections = {'details': {**DRAFT['details']}}
        self.assertEqual(self.put(self.body(sections=sections, record=record)).status_code, 200)
        stored = Listing.objects.get(pk=self.listing.pk).record
        self.assertEqual(stored['detail']['description'], 'Only this')
        self.assertEqual(stored['detail']['amenities'], ['AC'])
        self.assertEqual(stored['gallery'], RECORD['gallery'])
        self.assertEqual(stored['price'], '1500')

    def test_validation(self):
        bad = [
            {'basics': {**DRAFT['basics'], 'venueName': ''}},
            {'basics': {**DRAFT['basics'], 'phone': '123'}},
            {'details': {**DRAFT['details'], 'primaryCategory': 'Playzone Old'}},
            {'details': {**DRAFT['details'], 'openTime': '10:00', 'closeTime': '10:00'}},
            {'details': {**DRAFT['details'], 'openTime': '', 'closeTime': '', 'open24Hours': False}},
            {'photos': {'venuePhotos': [{'url': 'javascript:alert(1)'}]}},
            {'location': {**DRAFT['location'], 'pincode': '12'}},
        ]
        for sections in bad:
            self.assertEqual(self.put(self.body(sections=sections)).status_code, 400, sections)
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).name, 'Grand Hall')

    def test_category_change_to_the_new_model(self):
        sections = {'details': {**DRAFT['details'], 'primaryCategory': 'Open Theatre', 'categoryGroup': 'celebrate'}}
        record = {**RECORD, 'category': 'Open-air theatre'}
        self.assertEqual(self.put(self.body(sections=sections, record=record)).status_code, 200)
        self.assertEqual(Listing.objects.get(pk=self.listing.pk).category, 'Open-air theatre')
        sections = {'details': {**DRAFT['details'], 'primaryCategory': 'Gym', 'open24Hours': True}}
        record = {**RECORD, 'category': 'Gym'}
        self.assertEqual(self.put(self.body(sections=sections, record=record)).status_code, 200)
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual(listing.category, 'Gym')

    def test_stale_version_is_refused(self):
        body = self.body()
        Listing.objects.filter(pk=self.listing.pk).update(name='Vendor renamed')
        listing = Listing.objects.get(pk=self.listing.pk)
        listing.save()                                   # bumps updated_at
        response = self.put(body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'STALE_VERSION')

    def test_audited(self):
        self.put(self.body())
        entry = AuditEntry.objects.get(action='Edited venue details')
        self.assertEqual(entry.admin, 'Anita')
        self.assertEqual(entry.target_id, str(self.listing.pk))
        self.assertIn('name', entry.change)
        self.assertNotIn('123456789012', entry.change)

    def test_deleted_and_unit_listings_are_refused(self):
        sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall-2', name='Grand Hall — Hall 2',
            record={**RECORD, 'name': 'Grand Hall — Hall 2',
                    'detail': {**RECORD['detail'], 'unitOf': str(self.listing.pk), 'unitLabel': 'Hall 2'}},
            category='Party hall', locality='x', pincode='506001', status='live',
        )
        self.assertEqual(self.admin_client.get(f'/api/admin/venues/{sibling.pk}/edit').json()['code'], 'UNIT_LISTING')
        Listing.objects.filter(pk=self.listing.pk).update(status='deleted')
        self.assertEqual(self.admin_client.get(self.url).status_code, 409)

    def test_siblings_follow_and_resource_count_changes_are_refused(self):
        Listing.objects.filter(pk=self.listing.pk).update(record={**RECORD, 'detail': {**RECORD['detail'], 'unitCount': 2}})
        sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall-2', name='Grand Hall — Hall 2',
            record={**RECORD, 'name': 'Grand Hall — Hall 2', 'price': '900',
                    'detail': {**RECORD['detail'], 'unitCount': 2, 'unitOf': str(self.listing.pk), 'unitLabel': 'Hall 2'}},
            category='Party hall', locality='x', pincode='506001', status='live',
        )
        body = self.body()
        body['record']['detail']['unitCount'] = 3
        response = self.put(body)
        self.assertEqual(response.json()['code'], 'RESOURCES_CHANGED')
        body = self.body()
        body['record']['detail']['unitCount'] = 2
        self.assertEqual(self.put(body).status_code, 200)
        sibling.refresh_from_db()
        self.assertEqual(sibling.name, 'Grand Palace — Hall 2')
        self.assertEqual(sibling.record['detail']['description'], 'New text')
        self.assertEqual(sibling.record['detail']['unitOf'], str(self.listing.pk))
        self.assertEqual(sibling.record['price'], '900')                 # its own rate kept

    def test_form_posts_are_not_accepted(self):
        response = self.admin_client.put(self.url, 'sections=x', content_type='application/x-www-form-urlencoded')
        self.assertEqual(response.status_code, 415)
