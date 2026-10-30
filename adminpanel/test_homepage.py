"""
The super-admin-controlled Explore homepage (adminpanel/homepage.py).

Covers: only the super admin can change anything; banners/promotions are the
existing Settings.banners records (no second store) and multiple banners are
managed independently; hero banners need an image; unpublished, expired and
not-yet-active content is hidden; checkout redemption is unchanged; the
redemption history blocks unsafe deletes/edits; banner image format, size
and dimension rules match the real hero CSS; Top Events shows only live,
bookable, promoted events in the admin's order and schedule; section
switches/order/titles apply; featured venues; cache refresh without a
redeploy; and that no venue or event record is created, copied or lost.
"""
import datetime
import struct
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from events.models import Event, EventDate
from events.tests import make_event
from venues.models import Listing

from .homepage import (
    BANNER_IMAGE_SPEC,
    MOVABLE_SECTIONS,
    hero_frame,
)
from .image_info import image_size
from .models import Settings

TODAY = today_ist()
IMAGE = 'https://cdn.example.com/banner.jpg'


def png(width, height):
    """A PNG header with the given size (enough for the sniffers and the
    dimension reader — the bytes are never decoded)."""
    ihdr = struct.pack('>II', width, height) + b'\x08\x02\x00\x00\x00'
    return b'\x89PNG\r\n\x1a\n' + struct.pack('>I', 13) + b'IHDR' + ihdr + b'\x00' * 64


def jpeg(width, height):
    app0 = b'\xff\xe0' + struct.pack('>H', 16) + b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    sof0 = b'\xff\xc0' + struct.pack('>HBHHB', 17, 8, height, width, 3) + b'\x00' * 9
    return b'\xff\xd8' + app0 + sof0 + b'\xff\xd9'


def webp_vp8x(width, height):
    body = b'VP8X' + struct.pack('<I', 10) + b'\x00\x00\x00\x00' \
        + (width - 1).to_bytes(3, 'little') + (height - 1).to_bytes(3, 'little')
    return b'RIFF' + struct.pack('<I', len(body) + 4) + b'WEBP' + body + b'\x00' * 16


def webp_vp8(width, height):
    frame = b'\x00\x00\x00' + b'\x9d\x01\x2a' + struct.pack('<HH', width, height)
    body = b'VP8 ' + struct.pack('<I', len(frame)) + frame
    return b'RIFF' + struct.pack('<I', len(body) + 4) + b'WEBP' + body + b'\x00' * 16


def webp_vp8l(width, height):
    bits = (width - 1) | ((height - 1) << 14)
    frame = b'\x2f' + bits.to_bytes(4, 'little')
    body = b'VP8L' + struct.pack('<I', len(frame)) + frame
    return b'RIFF' + struct.pack('<I', len(body) + 4) + b'WEBP' + body + b'\x00' * 16


def jwt_client(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')
    return client


class HomepageTestBase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(
            phone='9990000101', name='Anita', email='anita@x.in',
            role=User.Role.ADMIN, password='pw',
        )
        self.vendor = User.objects.create_user(
            phone='9990000103', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9990000104', name='Asha', email='asha@x.in',
        )
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)
        self.public = APIClient()

    # -- helpers ----------------------------------------------------------
    def admin_get(self):
        return self.admin_client.get('/api/admin/homepage').data

    def create_banner(self, **fields):
        body = {'title': 'Weekend turf offer', 'image': IMAGE, 'published': True, **fields}
        response = self.admin_client.post('/api/admin/homepage/banners', body, format='json')
        self.assertIn(response.status_code, (201,), response.data)
        return response.data['banner']

    def homepage(self):
        return self.public.get('/api/homepage').data

    def venue(self, name, *, featured=False, status=Listing.Status.LIVE, unit_of=None, phone='9876543210'):
        detail = {'unitOf': unit_of} if unit_of else {}
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug=name.lower().replace(' ', '-'),
            record={'name': name, 'price': 900, 'image': 'https://x/v.jpg',
                    'contactPhone': phone, 'detail': detail},
            name=name, category='Party hall', locality='Hanamkonda', pincode='506001',
            status=status, featured=featured,
        )


class HomepagePermissionTests(HomepageTestBase):
    """Only the super admin may read or change homepage content."""

    WRITES = (
        ('put', '/api/admin/homepage/sections', {'revision': 0, 'sections': []}),
        ('post', '/api/admin/homepage/banners', {'title': 'x'}),
        ('patch', '/api/admin/homepage/banners/banner-0', {'title': 'x'}),
        ('delete', '/api/admin/homepage/banners/banner-0', None),
        ('post', '/api/admin/homepage/banners/reorder', {'placement': 'hero', 'ids': []}),
        ('post', '/api/admin/homepage/featured-events', {'eventId': str(uuid.uuid4())}),
        ('post', '/api/admin/homepage/featured-events/reorder', {'ids': []}),
        ('delete', f'/api/admin/homepage/featured-events/{uuid.uuid4()}', None),
        ('post', '/api/admin/homepage/featured-venues/reorder', {'ids': []}),
        ('get', '/api/admin/homepage', None),
        ('get', '/api/admin/ratings', None),
    )

    def attempt(self, client):
        out = []
        for method, url, body in self.WRITES:
            response = getattr(client, method)(url, body, format='json') if body is not None \
                else getattr(client, method)(url)
            out.append((url, response.status_code))
        return out

    def test_anonymous_customers_and_vendors_are_refused(self):
        Settings.load()
        before = Settings.objects.get(pk=1)
        session_customer = APIClient()
        session_customer.force_login(self.customer)
        session_vendor = APIClient()
        session_vendor.force_login(self.vendor)
        for label, client in (
            ('anonymous', APIClient()),
            ('customer JWT', jwt_client(self.customer)),
            ('vendor JWT', jwt_client(self.vendor)),
            ('customer session', session_customer),
            ('vendor session', session_vendor),
        ):
            for url, code in self.attempt(client):
                with self.subTest(who=label, url=url):
                    self.assertIn(code, (401, 403))
        after = Settings.objects.get(pk=1)
        self.assertEqual(
            (after.banners, after.homepage, after.homepage_revision),
            (before.banners, before.homepage, before.homepage_revision),
        )

    def test_banner_image_upload_is_admin_only(self):
        upload = SimpleUploadedFile('b.png', png(2400, 600), content_type='image/png')
        response = jwt_client(self.vendor).post(
            '/api/admin/homepage/banner-image', {'image': upload, 'slot': 'desktop'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertIn(response.status_code, (401, 403))


class BannerTests(HomepageTestBase):

    def test_banners_live_in_the_existing_settings_record(self):
        banner = self.create_banner(title='Diwali offer', type='percent', value=10, code='DIWALI10')
        stored = Settings.load().banners
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['id'], banner['id'])
        self.assertEqual(stored[0]['code'], 'DIWALI10')

    def test_new_banners_start_as_unpublished_drafts(self):
        response = self.admin_client.post(
            '/api/admin/homepage/banners', {'title': 'Draft', 'image': IMAGE}, format='json',
        )
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.data['banner']['published'])
        self.assertEqual(self.homepage()['hero'], [])
        self.assertEqual(self.public.get('/api/banners').data['banners'], [])

    def test_multiple_banners_are_managed_without_overwriting_each_other(self):
        first = self.create_banner(title='First')
        second = self.create_banner(title='Second')
        third = self.create_banner(title='Third')
        self.admin_client.patch(
            f'/api/admin/homepage/banners/{second["id"]}', {'text': 'Edited'}, format='json',
        )
        self.admin_client.delete(f'/api/admin/homepage/banners/{third["id"]}')
        stored = {b['id']: b for b in Settings.load().banners}
        self.assertEqual(set(stored), {first['id'], second['id']})
        self.assertEqual(stored[first['id']]['title'], 'First')
        self.assertEqual(stored[second['id']]['text'], 'Edited')

    def test_hero_banners_need_an_image(self):
        response = self.admin_client.post(
            '/api/admin/homepage/banners',
            {'title': 'Text only', 'published': True, 'showInHero': True}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'IMAGE_REQUIRED')
        # A promotion-only record needs no artwork.
        self.create_banner(title='Promo only', image='', showInHero=False,
                           type='flat', value=100, code='FLAT100')
        page = self.homepage()
        self.assertEqual(page['hero'], [])
        self.assertEqual([p['title'] for p in page['promotions']], ['Promo only'])

    def test_unsafe_urls_are_refused(self):
        for field, value in (('image', 'javascript:alert(1)'), ('ctaUrl', 'javascript:alert(1)'),
                             ('ctaUrl', '//evil.example.com'), ('imageMobile', 'data:image/png;base64,x')):
            with self.subTest(field=field):
                response = self.admin_client.post(
                    '/api/admin/homepage/banners', {'title': 'x', field: value}, format='json',
                )
                self.assertEqual(response.status_code, 400)

    def test_published_active_banners_show_and_others_are_hidden(self):
        self.create_banner(title='Live')
        self.create_banner(title='Expired', to=(TODAY - datetime.timedelta(days=1)).isoformat())
        self.create_banner(title='Scheduled', **{'from': (TODAY + datetime.timedelta(days=2)).isoformat()})
        self.create_banner(title='Unpublished', published=False)
        page = self.homepage()
        self.assertEqual([b['title'] for b in page['hero']], ['Live'])
        self.assertEqual([b['title'] for b in self.public.get('/api/banners').data['banners']], ['Live'])
        states = {b['title']: b['state'] for b in self.admin_get()['banners']}
        self.assertEqual(states, {'Live': 'live', 'Expired': 'expired',
                                  'Scheduled': 'scheduled', 'Unpublished': 'draft'})

    def test_public_payload_has_no_admin_bookkeeping(self):
        self.create_banner(title='Live')
        hero = self.homepage()['hero'][0]
        for key in ('published', 'updatedBy', 'updatedAt', 'createdAt', 'heroOrder'):
            self.assertNotIn(key, hero)
        self.assertNotIn('Anita', str(self.homepage()))

    def test_hero_order_follows_the_admins_reorder(self):
        a = self.create_banner(title='A')
        b = self.create_banner(title='B')
        c = self.create_banner(title='C')
        revision = self.admin_get()['revision']
        response = self.admin_client.post(
            '/api/admin/homepage/banners/reorder',
            {'revision': revision, 'placement': 'hero', 'ids': [c['id'], a['id'], b['id']]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual([x['title'] for x in self.homepage()['hero']], ['C', 'A', 'B'])

    def test_a_stale_reorder_is_refused_not_applied(self):
        a = self.create_banner(title='A')
        b = self.create_banner(title='B')
        stale = self.admin_get()['revision']
        self.create_banner(title='Another admin')           # someone saves in between
        response = self.admin_client.post(
            '/api/admin/homepage/banners/reorder',
            {'revision': stale, 'placement': 'hero', 'ids': [b['id'], a['id']]}, format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'STALE_HOMEPAGE')
        self.assertEqual([x['title'] for x in self.homepage()['hero']], ['A', 'B', 'Another admin'])

    def test_a_concurrent_edit_of_the_same_banner_is_refused(self):
        banner = self.create_banner(title='Original')
        loaded = banner['updatedAt']
        first = self.admin_client.patch(
            f'/api/admin/homepage/banners/{banner["id"]}',
            {'title': 'Admin one', 'expectedUpdatedAt': loaded}, format='json',
        )
        self.assertEqual(first.status_code, 200)
        second = self.admin_client.patch(
            f'/api/admin/homepage/banners/{banner["id"]}',
            {'title': 'Admin two', 'expectedUpdatedAt': loaded}, format='json',
        )
        self.assertEqual(second.status_code, 409)
        self.assertEqual(Settings.load().banners[0]['title'], 'Admin one')

    def test_codes_must_be_unique(self):
        self.create_banner(title='One', type='flat', value=50, code='SAVE50')
        response = self.admin_client.post(
            '/api/admin/homepage/banners',
            {'title': 'Two', 'type': 'percent', 'value': 5, 'code': 'save50', 'image': IMAGE},
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'DUPLICATE_CODE')

    def redeem(self, code):
        listing = self.venue('Redeem Hall')
        return Booking.objects.create(
            listing=listing, user=self.customer, date=TODAY, slots=['10:00 – 11:00'],
            amount=900, status='confirmed', razorpay_payment_id='pay_redeem',
            offer={'code': code, 'title': 'x', 'type': 'flat', 'value': 50, 'source': 'platform'},
            discount_amount=50,
        )

    def test_a_redeemed_promotion_cannot_be_deleted_or_change_its_code(self):
        banner = self.create_banner(title='Used', type='flat', value=50, code='USED50')
        self.redeem('USED50')
        delete = self.admin_client.delete(f'/api/admin/homepage/banners/{banner["id"]}')
        self.assertEqual(delete.status_code, 409)
        self.assertEqual(delete.data['code'], 'PROMOTION_REDEEMED')
        recode = self.admin_client.patch(
            f'/api/admin/homepage/banners/{banner["id"]}', {'code': 'NEW50'}, format='json',
        )
        self.assertEqual(recode.status_code, 409)
        # Content and the publish switch stay editable.
        edit = self.admin_client.patch(
            f'/api/admin/homepage/banners/{banner["id"]}',
            {'text': 'New artwork copy', 'published': False}, format='json',
        )
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(len(Settings.load().banners), 1)
        self.assertEqual(Booking.objects.filter(offer__code='USED50').count(), 1)
        self.assertEqual(self.admin_get()['banners'][0]['redemptions'], 1)

    def test_an_unused_banner_can_be_deleted(self):
        banner = self.create_banner(title='Unused', type='flat', value=50, code='UNUSED')
        self.assertEqual(
            self.admin_client.delete(f'/api/admin/homepage/banners/{banner["id"]}').status_code, 200,
        )
        self.assertEqual(Settings.load().banners, [])

    def test_checkout_redemption_rules_are_unchanged(self):
        from bookings.slots import SlotError
        from bookings.views import _apply_platform_offer

        self.create_banner(title='Hidden code', type='percent', value=10, code='HIDE10',
                           published=False, minAmount=500, maxDiscount=80)
        # Unpublishing only hides it from the homepage: the code still
        # validates exactly as before, with its guardrails.
        discount, applied = _apply_platform_offer(1000, {'code': 'HIDE10'}, self.customer)
        self.assertEqual((discount, applied['source']), (80, 'platform'))
        with self.assertRaises(SlotError):
            _apply_platform_offer(400, {'code': 'HIDE10'}, self.customer)

    def test_legacy_banners_keep_showing_until_edited(self):
        settings_obj = Settings.load()
        settings_obj.banners = [
            {'id': 1700000000000, 'title': 'Old text banner', 'text': 'Saved by the old form',
             'type': 'flat', 'value': 100, 'code': 'OLD100', 'from': '', 'to': ''},
        ]
        settings_obj.save()
        page = self.homepage()
        self.assertEqual([b['title'] for b in page['hero']], ['Old text banner'])
        self.assertEqual(page['hero'][0]['ctaLabel'], 'Explore venues')
        self.assertEqual([p['code'] for p in page['promotions']], ['OLD100'])
        self.assertEqual(self.public.get('/api/banners').data['banners'][0]['id'], 1700000000000)
        row = self.admin_get()['banners'][0]
        self.assertTrue(any('Text only' in issue for issue in row['issues']))
        # Unpublishing works without first adding an image...
        response = self.admin_client.patch(
            '/api/admin/homepage/banners/1700000000000', {'published': False}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Settings.load().banners[0]['id'], 1700000000000)   # id kept

    def test_featured_promotions_only_show_usable_offers(self):
        self.create_banner(title='Good', showInHero=False, type='percent', value=15, code='GOOD15',
                           category='sports')
        self.create_banner(title='Announcement', showInHero=False, type='none')
        self.create_banner(title='Not featured', showInHero=False, featuredPromotion=False,
                           type='flat', value=10, code='HIDDEN')
        self.create_banner(title='Expired', showInHero=False, type='flat', value=10, code='OLD',
                           to=(TODAY - datetime.timedelta(days=3)).isoformat())
        promos = self.homepage()['promotions']
        self.assertEqual([p['title'] for p in promos], ['Good', 'Announcement'])
        self.assertEqual(promos[0]['category'], 'sports')
        self.assertEqual(promos[1]['code'], '')

    def test_legacy_settings_save_with_a_stale_revision_is_refused(self):
        self.create_banner(title='Saved on the homepage page')
        response = self.admin_client.put(
            '/api/admin/settings', {'banners': [], 'homepageRevision': 0}, format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(Settings.load().banners), 1)


class BannerImageTests(HomepageTestBase):

    def upload(self, data, *, name='banner.png', content_type='image/png', slot='desktop', header=True):
        extra = {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'} if header else {}
        return self.admin_client.post(
            '/api/admin/homepage/banner-image',
            {'image': SimpleUploadedFile(name, data, content_type=content_type), 'slot': slot},
            **extra,
        )

    def test_dimension_reader_understands_every_accepted_format(self):
        self.assertEqual(image_size(png(2400, 600)), (2400, 600))
        self.assertEqual(image_size(jpeg(1920, 480)), (1920, 480))
        self.assertEqual(image_size(webp_vp8x(2400, 600)), (2400, 600))
        self.assertEqual(image_size(webp_vp8(1600, 400)), (1600, 400))
        self.assertEqual(image_size(webp_vp8l(1200, 600)), (1200, 600))
        self.assertIsNone(image_size(b'GIF89a' + b'\x00' * 40))
        self.assertIsNone(image_size(b'\xff\xd8\xff\xd9'))

    @mock.patch('venues.storage.upload_photo', return_value='https://cdn.example.com/banners/x.png')
    def test_a_good_banner_uploads_with_no_warnings(self, upload):
        response = self.upload(png(2400, 600))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data['width'], response.data['height']), (2400, 600))
        self.assertEqual(response.data['warnings'], [])
        self.assertTrue(upload.call_args[0][0].startswith('banners/'))

    @mock.patch('venues.storage.upload_photo', return_value='https://cdn.example.com/banners/x.jpg')
    def test_an_off_ratio_or_soft_image_uploads_with_warnings(self, _upload):
        response = self.upload(jpeg(1500, 500), name='b.jpg', content_type='image/jpeg')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data['warnings']), 2)       # 3:1, and below 1920×480

    @mock.patch('venues.storage.upload_photo')
    def test_invalid_files_are_refused_before_anything_is_stored(self, upload):
        cases = (
            ('wrong type', self.upload(b'GIF89a' + b'\x00' * 100, name='a.gif', content_type='image/gif')),
            ('lying type', self.upload(b'not an image at all', content_type='image/png')),
            ('too small', self.upload(png(800, 200))),
            ('extreme ratio', self.upload(png(4000, 400))),
            ('square', self.upload(png(1500, 1500))),
            ('phone too wide', self.upload(png(3000, 600), slot='mobile')),
            ('no header', self.upload(png(2400, 600), header=False)),
            ('bad slot', self.upload(png(2400, 600), slot='tablet')),
        )
        for label, response in cases:
            with self.subTest(case=label):
                self.assertEqual(response.status_code, 400)
        upload.assert_not_called()

    @mock.patch('venues.storage.upload_photo')
    def test_files_over_the_size_limit_are_refused(self, upload):
        big = png(2400, 600) + b'\x00' * BANNER_IMAGE_SPEC['maxBytes']
        response = self.upload(big)
        self.assertEqual(response.status_code, 400)
        self.assertIn('too large', response.data['detail'])
        upload.assert_not_called()

    @mock.patch('venues.storage.upload_photo', return_value='https://cdn.example.com/banners/m.webp')
    def test_phone_image_has_its_own_2_to_1_rule(self, _upload):
        response = self.upload(webp_vp8x(1200, 600), name='m.webp', content_type='image/webp', slot='mobile')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['warnings'], [])

    def test_guidance_matches_the_hero_css(self):
        # features/home/home.css: width = viewport − 80 (− 32 on phones),
        # min-height clamp(280px, 23vw, 420px), 168px on phones.
        self.assertEqual(hero_frame(1920), (1840, 420))
        self.assertEqual(hero_frame(1440), (1360, 331))
        self.assertEqual(hero_frame(1280), (1200, 294))
        self.assertEqual(hero_frame(1024), (944, 280))
        self.assertEqual(hero_frame(768), (688, 280))
        self.assertEqual(hero_frame(390), (358, 168))
        # Laptops and desktops sit between 4.0:1 and 4.4:1, so 4:1 is right.
        for viewport in (1280, 1366, 1440, 1536, 1680, 1920):
            width, height = hero_frame(viewport)
            self.assertTrue(4.0 <= width / height <= 4.4, viewport)
        desktop = BANNER_IMAGE_SPEC['slots']['desktop']
        self.assertEqual(desktop['recommended'], {'width': 2400, 'height': 600})
        self.assertEqual(desktop['ratio']['label'], '4:1')
        self.assertGreaterEqual(desktop['recommended']['width'], hero_frame(1920)[0])
        mobile = BANNER_IMAGE_SPEC['slots']['mobile']
        self.assertEqual(mobile['ratio']['label'], '2:1')
        for viewport in (360, 390, 414):
            width, height = hero_frame(viewport)
            self.assertTrue(1.9 <= width / height <= 2.3, viewport)
        # The admin page reads the same spec it previews with.
        self.assertEqual(self.admin_get()['imageSpec']['frames'][0], {
            'label': 'Large desktop', 'viewport': 1920, 'width': 1840, 'height': 420,
        })


class TopEventsTests(HomepageTestBase):

    def feature(self, event, **extra):
        return self.admin_client.post(
            '/api/admin/homepage/featured-events', {'eventId': str(event.pk), **extra}, format='json',
        )

    def top_names(self):
        return [e['name'] for e in self.homepage()['topEvents']]

    def test_only_live_bookable_featured_events_appear(self):
        live = make_event(name='Live Night')
        self.assertEqual(self.feature(live).status_code, 201)
        for state in (Event.Status.PENDING, Event.Status.REJECTED, Event.Status.DRAFT, Event.Status.PAUSED):
            event = make_event(name=f'{state} night', status=state)
            response = self.feature(event)
            self.assertEqual(response.status_code, 400, state)          # live only
            # Even forced in behind the API's back, it never shows.
            Event.objects.filter(pk=event.pk).update(featured=True)
        ended = make_event(name='Finished Night', days_ahead=(-2,))
        Event.objects.filter(pk=ended.pk).update(featured=True)
        self.assertEqual(self.top_names(), ['Live Night'])

    def test_order_limit_and_removal(self):
        events = [make_event(name=f'Night {i}') for i in range(4)]
        for event in events:
            self.feature(event)
        revision = self.admin_get()['revision']
        order = [events[2], events[0], events[3], events[1]]
        self.admin_client.post(
            '/api/admin/homepage/featured-events/reorder',
            {'revision': revision, 'ids': [str(e.pk) for e in order]}, format='json',
        )
        self.assertEqual(self.top_names(), ['Night 2', 'Night 0', 'Night 3', 'Night 1'])

        sections = self.admin_get()['sections']
        for s in sections:
            if s['key'] == 'topEvents':
                s['limit'] = 2
        self.admin_client.put('/api/admin/homepage/sections',
                              {'revision': self.admin_get()['revision'], 'sections': sections}, format='json')
        self.assertEqual(self.top_names(), ['Night 2', 'Night 0'])

        response = self.admin_client.delete(f'/api/admin/homepage/featured-events/{events[2].pk}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.top_names(), ['Night 0', 'Night 3'])
        removed = Event.objects.get(pk=events[2].pk)
        self.assertEqual((removed.status, removed.featured), (Event.Status.LIVE, False))
        self.assertEqual(Event.objects.count(), 4)                 # nothing deleted or copied

    def test_schedule_window(self):
        soon = make_event(name='Soon')
        self.feature(soon, **{'from': (TODAY + datetime.timedelta(days=1)).isoformat()})
        over = make_event(name='Over')
        self.feature(over, until=(TODAY - datetime.timedelta(days=1)).isoformat())
        now = make_event(name='Now')
        self.feature(now, **{'from': TODAY.isoformat(), 'until': TODAY.isoformat()})
        self.assertEqual(self.top_names(), ['Now'])
        states = {e['name']: e['state'] for e in self.admin_get()['featuredEvents']}
        self.assertEqual(states, {'Soon': 'scheduled', 'Over': 'ended', 'Now': 'showing'})
        bad = self.admin_client.patch(
            f'/api/admin/homepage/featured-events/{soon.pk}',
            {'from': '2026-12-10', 'until': '2026-12-01'}, format='json',
        )
        self.assertEqual(bad.status_code, 400)

    def test_old_featured_api_respects_the_schedule_too(self):
        hidden = make_event(name='Later')
        self.feature(hidden, **{'from': (TODAY + datetime.timedelta(days=5)).isoformat()})
        cache.clear()
        names = [e['name'] for e in self.public.get('/api/events', {'featured': '1'}).data['events']]
        self.assertEqual(names, [])

    def test_sold_out_or_closed_events_are_not_shown(self):
        closed = make_event(name='Closed Night')
        self.feature(closed)
        EventDate.objects.filter(event=closed).update(is_available=False)
        self.assertEqual(self.top_names(), [])

    def test_unpublishing_an_event_drops_it_from_the_homepage_at_once(self):
        event = make_event(name='Soon Gone')
        self.feature(event)
        self.assertEqual(self.top_names(), ['Soon Gone'])        # cached now
        self.admin_client.patch(f'/api/admin/events/{event.pk}', {'status': 'unpublish'}, format='json')
        self.assertEqual(self.top_names(), [])


class SectionsTests(HomepageTestBase):

    def save(self, sections, revision=None):
        return self.admin_client.put(
            '/api/admin/homepage/sections',
            {'revision': self.admin_get()['revision'] if revision is None else revision,
             'sections': sections}, format='json',
        )

    def test_defaults_render_the_existing_layout_in_the_brief_order(self):
        keys = [s['key'] for s in self.homepage()['sections']]
        self.assertEqual(keys[:3], ['hero', 'topEvents', 'featuredPromotions'])
        self.assertEqual(keys[3:], list(MOVABLE_SECTIONS))
        self.assertTrue(all(s['enabled'] for s in self.homepage()['sections']))

    def test_visibility_titles_and_order_are_applied(self):
        self.create_banner(title='Hero')
        sections = self.admin_get()['sections']
        by_key = {s['key']: s for s in sections}
        by_key['hero']['enabled'] = False
        by_key['featuredPromotions']['title'] = 'Deals this week'
        movable = [by_key[k] for k in ('venues', 'spotlight', 'newVenues', 'topPromotions')]
        # "How it works" was removed; an older admin client still sending it is not refused.
        movable.append({'key': 'howItWorks', 'enabled': True, 'title': 'How it works', 'subtitle': ''})
        # The fixed three are sent last on purpose: they keep their places.
        response = self.save(movable + [by_key['hero'], by_key['topEvents'], by_key['featuredPromotions']])
        self.assertEqual(response.status_code, 200, response.data)
        page = self.homepage()
        self.assertEqual(
            [s['key'] for s in page['sections']],
            ['hero', 'topEvents', 'featuredPromotions',
             'venues', 'spotlight', 'newVenues', 'topPromotions'],
        )
        sections = {s['key']: s for s in page['sections']}
        self.assertFalse(sections['hero']['enabled'])
        self.assertEqual(page['hero'], [])                       # disabled -> no data sent
        self.assertEqual(sections['featuredPromotions']['title'], 'Deals this week')

    def test_spotlight_and_newly_added_are_reported_as_merged_into_the_venue_list(self):
        # They used to repeat the main venue list; the keys stay (saved
        # layouts and older admin clients send them) but no row is drawn.
        by_key = {s['key']: s for s in self.admin_get()['sections']}
        self.assertEqual(by_key['spotlight']['mergedInto'], 'venues')
        self.assertEqual(by_key['newVenues']['mergedInto'], 'venues')
        self.assertIsNone(by_key['venues']['mergedInto'])
        # Blank = the customer site's automatic "Venues near you" heading.
        self.assertEqual(by_key['venues']['title'], '')
        # Switching "featured venues first" off still saves.
        by_key['spotlight']['enabled'] = False
        self.assertEqual(self.save(list(by_key.values())).status_code, 200)
        page = {s['key']: s for s in self.homepage()['sections']}
        self.assertFalse(page['spotlight']['enabled'])

    def test_the_main_venue_list_cannot_be_hidden(self):
        sections = self.admin_get()['sections']
        for s in sections:
            if s['key'] == 'venues':
                s['enabled'] = False
        self.assertEqual(self.save(sections).status_code, 400)

    def test_bad_input_and_stale_revisions_are_refused(self):
        sections = self.admin_get()['sections']
        self.assertEqual(self.save([{'key': 'nope'}]).status_code, 400)
        self.assertEqual(self.save([{'key': 'hero', 'title': 'x' * 200}]).status_code, 400)
        self.assertEqual(self.save([{'key': 'topEvents', 'limit': 99}]).status_code, 400)
        self.assertEqual(self.save(sections, revision=999).status_code, 409)


class FeaturedVenuesTests(HomepageTestBase):

    def test_featured_live_venues_lead_in_the_admins_order_without_contact_phones(self):
        a = self.venue('Alpha Hall', featured=True)
        b = self.venue('Beta Hall', featured=True)
        self.venue('Paused Hall', featured=True, status=Listing.Status.PAUSED)
        self.venue('Plain Hall')
        self.venue('Alpha Hall — Hall 2', featured=True, unit_of=str(a.pk))
        revision = self.admin_get()['revision']
        response = self.admin_client.post(
            '/api/admin/homepage/featured-venues/reorder',
            {'revision': revision, 'ids': [str(b.pk), str(a.pk)]}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        page = self.homepage()
        self.assertEqual([v['name'] for v in page['featuredVenues']], ['Beta Hall', 'Alpha Hall'])
        self.assertNotIn('9876543210', str(page))
        self.assertNotIn('contactPhone', page['featuredVenues'][0])

    def test_unfeaturing_through_the_venue_switch_updates_the_homepage_at_once(self):
        venue = self.venue('Gamma Hall', featured=True)
        self.assertEqual([v['name'] for v in self.homepage()['featuredVenues']], ['Gamma Hall'])
        self.admin_client.patch(f'/api/admin/venues/{venue.pk}', {'featured': False}, format='json')
        self.assertEqual(self.homepage()['featuredVenues'], [])
        self.assertTrue(Listing.objects.filter(pk=venue.pk, status=Listing.Status.LIVE).exists())


class HomepageCacheTests(HomepageTestBase):

    def test_published_changes_show_without_a_redeploy_or_cache_wait(self):
        self.assertEqual(self.homepage()['hero'], [])            # cached empty
        banner = self.create_banner(title='Fresh')
        self.assertEqual([b['title'] for b in self.homepage()['hero']], ['Fresh'])
        self.admin_client.patch(f'/api/admin/homepage/banners/{banner["id"]}',
                                {'published': False}, format='json')
        self.assertEqual(self.homepage()['hero'], [])

    def test_a_new_rating_refreshes_the_homepage_cards(self):
        event = make_event(name='Rated Night', days_ahead=(-1, 2))
        self.admin_client.post('/api/admin/homepage/featured-events',
                               {'eventId': str(event.pk)}, format='json')
        self.assertIsNone(self.homepage()['topEvents'][0]['rating'])
        from events.models import EventOrder
        past = event.dates.order_by('date').first()
        order = EventOrder.objects.create(
            event=event, event_date=past, user=self.customer, event_name=event.name,
            date=past.date, start_time=event.start_time, end_time=event.end_time,
            status='confirmed', razorpay_payment_id='pay_cache', amount=520,
        )
        response = jwt_client(self.customer).post(
            f'/api/events/{event.pk}/ratings', {'orderId': order.pk, 'stars': 5}, format='json',
        )
        self.assertEqual(response.status_code, 201)
        card = self.homepage()['topEvents'][0]
        self.assertEqual((card['rating'], card['ratingCount']), (5.0, 1))

    def test_the_homepage_does_not_repeat_queries_per_card(self):
        for i in range(6):
            self.admin_client.post('/api/admin/homepage/featured-events',
                                   {'eventId': str(make_event(name=f'Q{i}').pk)}, format='json')
        for i in range(6):
            self.venue(f'Venue {i}', featured=True)
        cache.clear()
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as cold:
            self.homepage()
        with CaptureQueriesContext(connection) as warm:
            self.homepage()
        # Warm: one query for the cache stamp. Cold: bounded, not per card.
        self.assertLessEqual(len(warm), 1)
        # 14 today for 6 events + 6 venues (one seat count per event night is
        # the existing inventory rule, events/inventory.py).
        self.assertLess(len(cold), 25)


class AdminRatingModerationTests(HomepageTestBase):

    def test_hiding_a_review_needs_a_reason_and_removes_it_from_the_average(self):
        from bookings.models import Rating

        listing = self.venue('Moderated Hall')
        booking = Booking.objects.create(
            listing=listing, user=self.customer, date=TODAY - datetime.timedelta(days=1),
            slots=['10:00 – 11:00'], amount=900, status='confirmed', razorpay_payment_id='pay_m',
        )
        rating = Rating.objects.create(booking=booking, listing=listing, user=self.customer,
                                       stars=1, review='Rude words here')
        listed = self.admin_client.get('/api/admin/ratings', {'written': '1'}).data
        self.assertEqual(listed['total'], 1)
        no_reason = self.admin_client.patch(f'/api/admin/ratings/{rating.pk}', {'hidden': True}, format='json')
        self.assertEqual(no_reason.status_code, 400)
        hidden = self.admin_client.patch(
            f'/api/admin/ratings/{rating.pk}', {'hidden': True, 'reason': 'Abusive'}, format='json',
        )
        self.assertTrue(hidden.data['hidden'])
        from bookings.ratings import venue_rating
        self.assertEqual(venue_rating(listing), (None, 0))
        self.assertTrue(Rating.objects.filter(pk=rating.pk).exists())     # kept, not deleted
        restored = self.admin_client.patch(f'/api/admin/ratings/{rating.pk}', {'hidden': False}, format='json')
        self.assertFalse(restored.data['hidden'])
        self.assertEqual(venue_rating(listing), (1.0, 1))


class BannerImagePositionTests(HomepageTestBase):
    """imagePosition / imageMobilePosition — which part of the artwork stays
    in view when a frame has to crop it. Optional; old banners read centre."""

    def test_positions_are_stored_and_served(self):
        self.create_banner(imageMobile='https://cdn.example.com/phone.jpg',
                           imagePosition='left', imageMobilePosition='top')
        banner = self.public.get('/api/banners').data['banners'][0]
        self.assertEqual((banner['imagePosition'], banner['imageMobilePosition']), ('left', 'top'))

    def test_old_banners_default_to_centre(self):
        self.create_banner()
        banner = self.public.get('/api/banners').data['banners'][0]
        self.assertEqual((banner['imagePosition'], banner['imageMobilePosition']), ('center', 'center'))

    def test_unknown_positions_are_refused(self):
        response = self.admin_client.post('/api/admin/homepage/banners', {
            'title': 'x', 'image': IMAGE, 'imagePosition': '50% 50%; background:url(x)',
        }, format='json')
        self.assertEqual(response.status_code, 400)
