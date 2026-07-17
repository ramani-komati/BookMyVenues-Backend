"""
Tests for the venue-registration draft endpoints (contract Group 4).
"""
import uuid
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
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


class DraftPhotoTests(DraftTestBase):
    """Storage calls are mocked — tests never touch Supabase."""

    def setUp(self):
        super().setUp()
        self.draft = VenueDraft.objects.create(vendor=self.vendor)
        upload_patcher = patch(
            'venues.views.upload_photo',
            side_effect=lambda path, content, ct: f'https://cdn.example/{path}',
        )
        delete_patcher = patch('venues.views.delete_photo')
        self.mock_upload = upload_patcher.start()
        self.mock_delete = delete_patcher.start()
        self.addCleanup(upload_patcher.stop)
        self.addCleanup(delete_patcher.stop)

    def upload(self, gallery='venuePhotos', name='hall.jpg',
               content=b'fake-image-bytes', content_type='image/jpeg'):
        file = SimpleUploadedFile(name, content, content_type=content_type)
        return self.client.post(
            f'/api/venues/drafts/{self.draft.id}/photos',
            {'file': file, 'gallery': gallery},
            format='multipart',
        )

    def test_upload_happy_path(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['gallery'], 'venuePhotos')
        photo = response.data['photo']
        self.assertEqual(photo['name'], 'hall.jpg')
        self.assertTrue(photo['url'].startswith('https://cdn.example/'))

        self.draft.refresh_from_db()
        self.assertEqual(len(self.draft.data['photos']['venuePhotos']), 1)
        # First venue photo completes the photos bucket -> 20%.
        self.assertEqual(response.data['completion'], 20)

    def test_missing_file_rejected(self):
        response = self.client.post(
            f'/api/venues/drafts/{self.draft.id}/photos',
            {'gallery': 'venuePhotos'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 400)

    def test_bad_gallery_rejected(self):
        response = self.upload(gallery='wrongGallery')
        self.assertEqual(response.status_code, 400)

    def test_non_image_rejected(self):
        response = self.upload(name='virus.txt', content_type='text/plain')
        self.assertEqual(response.status_code, 400)
        self.mock_upload.assert_not_called()

    def test_oversized_image_rejected(self):
        big = b'x' * (5 * 1024 * 1024 + 1)
        response = self.upload(content=big)
        self.assertEqual(response.status_code, 413)
        self.mock_upload.assert_not_called()

    def test_venue_gallery_cap_is_5(self):
        for _ in range(5):
            self.assertEqual(self.upload().status_code, 201)
        response = self.upload()
        self.assertEqual(response.status_code, 400)
        self.assertIn('Maximum 5', response.data['message'])

    def test_service_gallery_cap_is_10(self):
        for _ in range(10):
            self.assertEqual(self.upload(gallery='serviceImages').status_code, 201)
        response = self.upload(gallery='serviceImages')
        self.assertEqual(response.status_code, 400)

    def test_storage_failure_returns_502(self):
        from venues.storage import StorageError
        with patch('venues.views.upload_photo', side_effect=StorageError('down')):
            response = self.upload()
        self.assertEqual(response.status_code, 502)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.data['photos']['venuePhotos'], [])

    def test_foreign_draft_404(self):
        foreign = VenueDraft.objects.create(vendor=self.other_vendor)
        file = SimpleUploadedFile('a.jpg', b'x', content_type='image/jpeg')
        response = self.client.post(
            f'/api/venues/drafts/{foreign.id}/photos',
            {'file': file, 'gallery': 'venuePhotos'},
            format='multipart',
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_photo(self):
        photo_id = self.upload().data['photo']['id']
        response = self.client.delete(
            f'/api/venues/drafts/{self.draft.id}/photos/{photo_id}?gallery=venuePhotos'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['photoId'], photo_id)
        self.mock_delete.assert_called_once()
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.data['photos']['venuePhotos'], [])

    def test_delete_unknown_photo_404(self):
        response = self.client.delete(
            f'/api/venues/drafts/{self.draft.id}/photos/nope123?gallery=venuePhotos'
        )
        self.assertEqual(response.status_code, 404)

    def test_delete_requires_gallery_param(self):
        photo_id = self.upload().data['photo']['id']
        response = self.client.delete(
            f'/api/venues/drafts/{self.draft.id}/photos/{photo_id}'
        )
        self.assertEqual(response.status_code, 400)


LISTING_RECORD = {
    'name': 'Grand Palace Hall',
    'category': 'hall',
    'locality': 'Indiranagar',
    'location': 'Indiranagar, Bengaluru',
    'pincode': '560038',
    'price': 1200,
    'unit': 'hour',
    'meta': '200 guests',
    'image': 'https://cdn.example/cover.jpg',
    'gallery': ['https://cdn.example/1.jpg', 'https://cdn.example/2.jpg'],
    'detail': {
        'description': 'A lovely hall.',
        'amenities': ['WiFi', 'AC'],
        'parking': True,
        'dining': False,
        'capacity': '200',
        'packages': [{'label': 'Gold', 'price': 4999, 'duration': '3 hrs', 'details': 'Decor + DJ'}],
        'sports': [],
        'addons': [{'name': 'Photographer', 'price': 2000}],
        'occasions': ['Wedding'],
        'contactPhone': '9876543210',
        'address': '12 MG Road',
        'mapsLink': 'https://maps.google.com/?q=x',
    },
}


class ListingTestBase(DraftTestBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()  # public views cache for 60s — isolate tests
        self.draft = self.make_full_draft()

    def publish(self, record=None, **overrides):
        body = {**(record or LISTING_RECORD), 'id': str(self.draft.id), **overrides}
        return self.client.post('/api/vendors/me/listings', body, format='json')


class PublishListingTests(ListingTestBase):
    def test_publish_own_draft_creates_live_listing(self):
        response = self.publish()
        self.assertEqual(response.status_code, 201)
        listing = response.data['listing']
        self.assertEqual(listing['status'], 'live')
        self.assertEqual(listing['id'], str(self.draft.id))

        from venues.models import Listing
        row = Listing.objects.get(pk=self.draft.id)
        self.assertEqual(row.name, 'Grand Palace Hall')
        self.assertEqual(row.slug, 'grand-palace-hall')
        self.assertEqual(row.vendor, self.vendor)

    def test_republish_updates_never_duplicates(self):
        self.publish()
        response = self.publish(name='Renamed Palace')
        self.assertEqual(response.status_code, 201)

        from venues.models import Listing
        self.assertEqual(Listing.objects.count(), 1)
        self.assertEqual(Listing.objects.get(pk=self.draft.id).name, 'Renamed Palace')

    def test_update_without_gallery_keeps_old_photos(self):
        self.publish()
        record = {**LISTING_RECORD}
        record.pop('gallery')
        response = self.publish(record=record)
        self.assertEqual(
            response.data['listing']['gallery'], LISTING_RECORD['gallery']
        )

    def test_cannot_publish_foreign_draft_id(self):
        foreign_draft = VenueDraft.objects.create(vendor=self.other_vendor)
        response = self.publish(id=str(foreign_draft.id))
        self.assertEqual(response.status_code, 403)

    def test_cannot_update_foreign_listing(self):
        self.publish()
        self.client.force_authenticate(user=self.other_vendor)
        response = self.publish()
        self.assertEqual(response.status_code, 403)

    def test_missing_id_rejected(self):
        response = self.client.post(
            '/api/vendors/me/listings', {'name': 'No id'}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_customer_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        response = self.publish()
        self.assertEqual(response.status_code, 403)


class PublicBrowsingTests(ListingTestBase):
    def setUp(self):
        super().setUp()
        self.publish()
        self.client.force_authenticate(user=None)  # public = no auth

    def clear_cache(self):
        from django.core.cache import cache
        cache.clear()

    def test_list_returns_live_venues(self):
        response = self.client.get('/api/venues')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 1)
        venue = response.data['venues'][0]
        self.assertEqual(venue['name'], 'Grand Palace Hall')
        self.assertEqual(venue['status'], 'live')
        self.assertNotIn('gallery', venue)  # summaries stay light
        self.assertNotIn('detail', venue)

    def test_non_live_listing_hidden(self):
        from venues.models import Listing
        Listing.objects.update(status=Listing.Status.PENDING)
        self.clear_cache()
        response = self.client.get('/api/venues')
        self.assertEqual(response.data['total'], 0)

    def test_search_by_name(self):
        response = self.client.get('/api/venues?q=palace')
        self.assertEqual(response.data['total'], 1)
        self.clear_cache()
        response = self.client.get('/api/venues?q=nomatch')
        self.assertEqual(response.data['total'], 0)

    def test_filter_by_category_and_pincode(self):
        response = self.client.get('/api/venues?category=hall&pincode=560038')
        self.assertEqual(response.data['total'], 1)
        self.clear_cache()
        response = self.client.get('/api/venues?pincode=999999')
        self.assertEqual(response.data['total'], 0)

    def test_limit_over_50_rejected(self):
        response = self.client.get('/api/venues?limit=51')
        self.assertEqual(response.status_code, 400)

    def test_bad_limit_rejected(self):
        response = self.client.get('/api/venues?limit=abc')
        self.assertEqual(response.status_code, 400)

    def test_bad_sort_rejected(self):
        response = self.client.get('/api/venues?sort=weird')
        self.assertEqual(response.status_code, 400)

    def test_detail_by_id_and_slug(self):
        by_id = self.client.get(f'/api/venues/{self.draft.id}')
        self.assertEqual(by_id.status_code, 200)
        self.assertIn('gallery', by_id.data)
        self.assertIn('detail', by_id.data)

        by_slug = self.client.get('/api/venues/grand-palace-hall')
        self.assertEqual(by_slug.status_code, 200)
        self.assertEqual(by_slug.data['name'], 'Grand Palace Hall')

    def test_detail_unknown_404(self):
        response = self.client.get('/api/venues/no-such-venue')
        self.assertEqual(response.status_code, 404)

    def test_detail_hidden_when_not_live(self):
        from venues.models import Listing
        Listing.objects.update(status=Listing.Status.PENDING)
        self.clear_cache()
        response = self.client.get('/api/venues/grand-palace-hall')
        self.assertEqual(response.status_code, 404)


class OldWizardRemovedTests(DraftTestBase):
    def test_old_vendor_venues_endpoint_gone(self):
        response = self.client.get('/api/v1/vendor/venues')
        self.assertEqual(response.status_code, 404)

    def test_old_payout_endpoint_gone(self):
        response = self.client.get('/api/v1/vendor/payout')
        self.assertEqual(response.status_code, 404)
