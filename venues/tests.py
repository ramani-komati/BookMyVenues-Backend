"""
Tests for the venue-registration draft endpoints (contract Group 4).
"""
import uuid

from rest_framework.test import APITestCase

from accounts.models import User

from .completion import compute_completion
from .models import VenueDraft, empty_draft_data

FULL_DATA = {
    'basics': {'venueName': 'Grand Palace Hall', 'phone': '9876543210'},
    'location': {
        'houseStreet': '12 MG Road',
        'pincode': '560001',
        'stateName': 'Karnataka',
        'mapsLink': 'https://maps.google.com/?q=grand+palace',
    },
    'details': {
        'primaryCategory': 'hall',
        'capacity': '120',
        'amenities': ['WiFi', 'AC'],
        'packages': [{'label': 'Gold', 'price': '4999'}],
    },
    'payout': {
        'accountHolder': 'Ravi Kumar',
        'bankName': 'HDFC Bank',
        'accountNumber': '123456789012',
        'ifsc': 'HDFC0001234',
        'phone': '9876543210',
    },
    'photos': {'venuePhotos': [{'id': 'p1', 'url': 'https://cdn/x.jpg'}], 'serviceImages': []},
}


class DraftTestBase(APITestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000001', name='Vendor One', email='v1@example.com',
            role=User.Role.VENDOR,
        )
        self.other_vendor = User.objects.create_user(
            phone='9000000002', name='Vendor Two', email='v2@example.com',
            role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9000000003', name='Customer', email='c@example.com',
        )
        self.client.force_authenticate(user=self.vendor)

    def create_draft(self, body=None):
        return self.client.post('/api/venues/drafts', body or {}, format='json')

    def make_full_draft(self):
        """A draft that passes every submit gate."""
        return VenueDraft.objects.create(vendor=self.vendor, data=FULL_DATA)


class DraftCreateTests(DraftTestBase):
    def test_create_empty_draft(self):
        response = self.create_draft()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['completion'], 0)
        self.assertIn('draftId', response.data)
        self.assertEqual(
            set(response.data['draft'].keys()),
            {'basics', 'location', 'details', 'payout', 'photos'},
        )

    def test_create_with_initial_sections(self):
        response = self.create_draft({'basics': FULL_DATA['basics']})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['completion'], 20)  # basics complete
        self.assertEqual(response.data['draft']['basics']['venueName'], 'Grand Palace Hall')

    def test_create_with_bad_format_rejected(self):
        response = self.create_draft({'location': {'pincode': '12'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('message', response.data)

    def test_customer_role_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        response = self.create_draft()
        self.assertEqual(response.status_code, 403)

    def test_anonymous_unauthorized(self):
        self.client.force_authenticate(user=None)
        response = self.create_draft()
        self.assertEqual(response.status_code, 401)


class DraftGetDeleteTests(DraftTestBase):
    def test_get_returns_saved_data_and_status(self):
        draft = self.make_full_draft()
        response = self.client.get(f'/api/venues/drafts/{draft.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'draft')
        self.assertEqual(response.data['completion'], 100)
        self.assertEqual(response.data['draft'], FULL_DATA)

    def test_foreign_draft_is_404(self):
        draft = VenueDraft.objects.create(vendor=self.other_vendor)
        response = self.client.get(f'/api/venues/drafts/{draft.id}')
        self.assertEqual(response.status_code, 404)

    def test_delete_wipes_draft(self):
        draft = self.make_full_draft()
        response = self.client.delete(f'/api/venues/drafts/{draft.id}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['deleted'], True)
        self.assertFalse(VenueDraft.objects.filter(pk=draft.id).exists())


class DraftSectionTests(DraftTestBase):
    def patch_section(self, draft, section, body):
        return self.client.patch(
            f'/api/venues/drafts/{draft.id}/sections/{section}', body, format='json'
        )

    def test_patch_merges_and_returns_completion(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.patch_section(draft, 'basics', FULL_DATA['basics'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['section'], 'basics')
        self.assertEqual(response.data['completion'], 20)

        draft.refresh_from_db()
        self.assertEqual(draft.data['basics']['venueName'], 'Grand Palace Hall')

    def test_patch_is_shallow_merge_not_replace(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        self.patch_section(draft, 'basics', {'venueName': 'Hall A'})
        self.patch_section(draft, 'basics', {'phone': '9876543210'})
        draft.refresh_from_db()
        # First key must survive the second save.
        self.assertEqual(draft.data['basics']['venueName'], 'Hall A')
        self.assertEqual(draft.data['basics']['phone'], '9876543210')

    def test_unknown_section_rejected(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.patch_section(draft, 'bogus', {'x': 1})
        self.assertEqual(response.status_code, 400)

    def test_bad_pincode_rejected(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.patch_section(draft, 'location', {'pincode': '99'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('6 digits', response.data['message'])

    def test_bad_ifsc_rejected(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.patch_section(draft, 'payout', {'ifsc': 'BAD'})
        self.assertEqual(response.status_code, 400)

    def test_empty_values_allowed(self):
        """Autosave sends half-typed forms — empty strings must not error."""
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.patch_section(draft, 'payout', {'ifsc': '', 'accountNumber': ''})
        self.assertEqual(response.status_code, 200)


class DraftSubmitTests(DraftTestBase):
    def submit(self, draft):
        return self.client.post(f'/api/venues/drafts/{draft.id}/submit')

    def test_incomplete_draft_lists_missing_fields(self):
        draft = VenueDraft.objects.create(vendor=self.vendor)
        response = self.submit(draft)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Venue name', response.data['message'])
        self.assertIn('Pincode', response.data['message'])

    def test_complete_draft_goes_pending(self):
        draft = self.make_full_draft()
        response = self.submit(draft)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'pending')
        self.assertIsNotNone(response.data['submittedAt'])

    def test_submit_is_idempotent(self):
        draft = self.make_full_draft()
        first = self.submit(draft)
        second = self.submit(draft)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data['submittedAt'], first.data['submittedAt'])

    def test_playzone_requires_sport(self):
        data = {**FULL_DATA, 'details': {
            'primaryCategory': 'playzone',
            'capacity': '40',
            'amenities': ['Parking'],
            'packages': [{'label': 'X', 'price': '100'}],  # packages don't count
        }}
        draft = VenueDraft.objects.create(vendor=self.vendor, data=data)
        response = self.submit(draft)
        self.assertEqual(response.status_code, 400)
        self.assertIn('sport', response.data['message'])


class DraftReopenSeedTests(DraftTestBase):
    def test_reopen_returns_to_draft(self):
        draft = self.make_full_draft()
        self.client.post(f'/api/venues/drafts/{draft.id}/submit')
        response = self.client.post(f'/api/venues/drafts/{draft.id}/reopen')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'draft')

    def test_seed_creates_draft_under_given_id(self):
        listing_id = uuid.uuid4()
        response = self.client.post(
            f'/api/venues/drafts/{listing_id}/seed',
            {'basics': FULL_DATA['basics']},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['draftId'], str(listing_id))
        self.assertEqual(response.data['status'], 'draft')
        draft = VenueDraft.objects.get(pk=listing_id)
        self.assertEqual(draft.data['basics']['venueName'], 'Grand Palace Hall')

    def test_seed_existing_draft_updates_it(self):
        draft = self.make_full_draft()
        response = self.client.post(
            f'/api/venues/drafts/{draft.id}/seed',
            {'basics': {'venueName': 'Renamed Hall'}},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        draft.refresh_from_db()
        self.assertEqual(draft.data['basics']['venueName'], 'Renamed Hall')
        # Untouched keys survive (shallow merge).
        self.assertEqual(draft.data['basics']['phone'], '9876543210')
        self.assertEqual(VenueDraft.objects.count(), 1)  # updated, NOT duplicated

    def test_seed_foreign_draft_404(self):
        draft = VenueDraft.objects.create(vendor=self.other_vendor)
        response = self.client.post(f'/api/venues/drafts/{draft.id}/seed', {}, format='json')
        self.assertEqual(response.status_code, 404)


class CompletionTests(DraftTestBase):
    def test_each_bucket_worth_20(self):
        draft = VenueDraft.objects.create(vendor=self.vendor, data=empty_draft_data())
        self.assertEqual(compute_completion(draft)[0], 0)

        for i, section in enumerate(['basics', 'location', 'details', 'payout', 'photos']):
            draft.data[section] = FULL_DATA[section]
            self.assertEqual(compute_completion(draft)[0], (i + 1) * 20)

    def test_full_draft_has_no_missing(self):
        draft = self.make_full_draft()
        percent, missing = compute_completion(draft)
        self.assertEqual(percent, 100)
        self.assertEqual(missing, [])


class OldWizardRemovedTests(DraftTestBase):
    def test_old_vendor_venues_endpoint_gone(self):
        response = self.client.get('/api/v1/vendor/venues')
        self.assertEqual(response.status_code, 404)

    def test_old_payout_endpoint_gone(self):
        response = self.client.get('/api/v1/vendor/payout')
        self.assertEqual(response.status_code, 404)
