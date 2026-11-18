"""
Listing image rules (venues/image_specs.py) and the three upload endpoints
that apply them: vendor venue photos, vendor event images, admin uploads.

Sizes used below are real covers / posters from the live catalogue
(2026-10-25). Storage is always mocked.
"""
import struct
import zlib
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User
from events.tests import auth, make_vendor
from venues.image_specs import LISTING_IMAGE_SPEC, listing_image_check
from venues.models import VenueDraft


def png(width, height):
    """A PNG header with the given size — enough for the size reader."""
    ihdr = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    chunk = b'IHDR' + ihdr
    return (b'\x89PNG\r\n\x1a\n' + struct.pack('>I', len(ihdr)) + chunk
            + struct.pack('>I', zlib.crc32(chunk)) + b'\x00' * 32)


class ListingImageCheckTests(SimpleTestCase):
    def test_spec_matches_the_customer_site(self):
        # Same numbers as src/utils/imageSpecs.js UPLOAD_SPECS.
        self.assertEqual(LISTING_IMAGE_SPEC['recommended'], {'width': 1600, 'height': 1067})
        self.assertEqual(LISTING_IMAGE_SPEC['minimum'], {'width': 400, 'height': 300})
        self.assertEqual(LISTING_IMAGE_SPEC['frame']['label'], '3:2')

    def test_a_3_2_photo_passes_without_warnings(self):
        problem, info = listing_image_check(png(1600, 1067))
        self.assertIsNone(problem)
        self.assertEqual((info['width'], info['height'], info['warnings']), (1600, 1067, []))

    def test_too_small_or_absurdly_large_is_refused(self):
        self.assertIn('too small', listing_image_check(png(382, 355))[0])
        self.assertIn('8000px', listing_image_check(png(9000, 3000))[0])
        # The minimum is orientation-free.
        self.assertIsNone(listing_image_check(png(300, 400))[0])

    def test_shape_only_ever_warns(self):
        problem, info = listing_image_check(png(1200, 1200))           # square cover
        self.assertIsNone(problem)
        self.assertIn('trim', info['warnings'][0])
        problem, info = listing_image_check(png(1536, 473))            # panorama
        self.assertIsNone(problem)
        self.assertIn('whole', info['warnings'][0])

    def test_posters_are_shown_whole_sooner(self):
        _, info = listing_image_check(png(1600, 1600), 'poster')       # square poster
        self.assertIn('whole', info['warnings'][0])

    def test_soft_images_are_flagged(self):
        _, info = listing_image_check(png(654, 436))
        self.assertIn('soft', info['warnings'][-1])

    def test_an_unreadable_header_is_accepted_without_advice(self):
        problem, info = listing_image_check(b'\xff\xd8\xff\xe0' + b'opaque bytes')
        self.assertIsNone(problem)
        self.assertEqual(info, {'width': None, 'height': None, 'warnings': []})


class VenuePhotoUploadPixelTests(APITestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000101', name='Vendor', email='pix@example.com', role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=self.vendor)
        self.draft = VenueDraft.objects.create(vendor=self.vendor)
        patcher = patch('venues.views.upload_photo',
                        side_effect=lambda path, content, ct: f'https://cdn.example/{path}')
        self.store = patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, content):
        return self.client.post(
            f'/api/venues/drafts/{self.draft.id}/photos',
            {'file': SimpleUploadedFile('hall.png', content, content_type='image/png'),
             'gallery': 'venuePhotos'},
            format='multipart',
        )

    def test_tiny_photo_is_refused_and_nothing_is_stored(self):
        r = self.upload(png(382, 355))
        self.assertEqual(r.status_code, 400)
        self.assertIn('too small', str(r.data))
        self.store.assert_not_called()
        self.draft.refresh_from_db()
        self.assertEqual((self.draft.data.get('photos') or {}).get('venuePhotos', []), [])

    def test_good_photo_uploads_with_its_size_and_advice(self):
        r = self.upload(png(1200, 1200))
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['image']['width'], r.data['image']['height']), (1200, 1200))
        self.assertTrue(r.data['image']['warnings'])
        # The stored bytes are exactly what was sent.
        self.assertEqual(self.store.call_args[0][1], png(1200, 1200))


class EventAndAdminUploadPixelTests(APITestCase):
    def test_vendor_event_image_is_checked_too(self):
        client = auth(make_vendor(phone='9000000102'))
        with patch('events.vendor_views.upload_photo', return_value='https://cdn.example/e.png') as store:
            r = client.post(reverse('vendor-event-images'),
                            {'file': SimpleUploadedFile('p.png', png(300, 200), content_type='image/png')},
                            format='multipart')
            self.assertEqual(r.status_code, 400)
            store.assert_not_called()
            r = client.post(reverse('vendor-event-images'),
                            {'file': SimpleUploadedFile('p.png', png(1600, 1600), content_type='image/png')},
                            format='multipart')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['url'], 'https://cdn.example/e.png')
        self.assertIn('whole', r.data['image']['warnings'][0])

    def test_admin_upload_is_checked_too(self):
        admin = User.objects.create_user(
            phone='9990000099', name='Admin', email='anita@bookmyvenues.in',
            role=User.Role.ADMIN, password='StrongPass123',
        )
        self.client.force_authenticate(user=admin)
        header = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}
        with patch('adminpanel.views.upload_photo', return_value='https://cdn.example/a.png') as store:
            r = self.client.post('/api/admin/uploads',
                                 {'image': SimpleUploadedFile('a.png', png(200, 150), content_type='image/png')},
                                 format='multipart', **header)
            self.assertEqual(r.status_code, 400)
            store.assert_not_called()
            r = self.client.post('/api/admin/uploads',
                                 {'image': SimpleUploadedFile('a.png', png(1600, 1067), content_type='image/png')},
                                 format='multipart', **header)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['image'], {'width': 1600, 'height': 1067, 'warnings': []})

    def test_customers_cannot_upload_listing_images(self):
        customer = User.objects.create_user(phone='9000000103', name='C', email='c103@example.com')
        r = auth(customer).post(reverse('vendor-event-images'),
                                {'file': SimpleUploadedFile('p.png', png(1600, 1067), content_type='image/png')},
                                format='multipart')
        self.assertIn(r.status_code, (401, 403))
