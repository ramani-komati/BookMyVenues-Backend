"""
Tests for the super-admin panel (Phase 1: auth + bootstrap).

The 2Factor SMS call is MOCKED — no real SMS, and the code is captured
in-memory (never printed).
"""
import datetime
import uuid
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from adminpanel.models import AuditEntry, Payout, Review
from bookings.models import Booking
from bookings.slots import today_ist
from venues.models import Listing, VenueDraft

ADMIN_EMAIL = 'anita@bookmyvenues.in'
ADMIN_PASSWORD = 'StrongPass123'


class AdminAuthTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # throttle counters live in the cache — isolate tests
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.sent = {}

        def fake_send(phone, code):
            self.sent[phone] = code

        patcher = patch('adminpanel.views.send_otp_sms', side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def login(self, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
        return self.client.post(
            '/api/admin/auth/login',
            {'email': email, 'password': password},
            format='json',
        )

    def verify(self, otp=None, email=ADMIN_EMAIL):
        return self.client.post(
            '/api/admin/auth/verify-otp',
            {'email': email, 'otp': otp or self.sent['9990000001']},
            format='json',
        )

    def test_login_triggers_otp(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'otpRequired': True})
        self.assertIn('9990000001', self.sent)

    def test_login_is_rate_limited(self):
        # The admin URL is discoverable, so password guessing must be capped.
        from django.core.cache import cache
        cache.clear()
        codes = [self.login(password='wrong').status_code for _ in range(12)]
        self.assertIn(429, codes)  # throttled before 12 guesses land

    def test_wrong_password_rejected(self):
        response = self.login(password='nope')
        self.assertEqual(response.status_code, 400)
        self.assertIn('detail', response.data)  # NOT "message"

    def test_non_admin_cannot_login(self):
        User.objects.create_user(
            phone='9990000002', name='V', email='v@x.in',
            role=User.Role.VENDOR, password='vendorpass',
        )
        response = self.login(email='v@x.in', password='vendorpass')
        self.assertEqual(response.status_code, 400)

    def test_full_login_flow_sets_session(self):
        self.login()
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.data)
        # Session cookie is now set -> the protected bootstrap works.
        boot = self.client.get('/api/admin/bootstrap')
        self.assertEqual(boot.status_code, 200)

    def test_wrong_otp_rejected(self):
        self.login()
        if self.sent['9990000001'] == '000000':
            return  # astronomically unlikely code collision
        response = self.verify(otp='000000')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['detail'], 'That code is not right.')

    def test_logout_clears_session(self):
        self.login()
        self.verify()
        out = self.client.post('/api/admin/auth/logout')
        self.assertIn(out.status_code, (200, 204))
        blocked = self.client.get('/api/admin/bootstrap')
        self.assertNotEqual(blocked.status_code, 200)


class AdminBootstrapTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9990000004', name='Asha', email='asha@x.in',
        )
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall',
            record={'name': 'Grand Hall', 'price': 1200, 'image': 'https://x/c.jpg',
                    'detail': {'capacity': '200', 'amenities': ['AC']}},
            name='Grand Hall', category='hall', locality='HSR', pincode='560102',
        )
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['19:00 – 20:00'], amount=1220, customer_name='Asha',
            venue_name='Grand Hall',
        )

    def test_bootstrap_requires_admin(self):
        response = self.client.get('/api/admin/bootstrap')
        self.assertNotEqual(response.status_code, 200)

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.get('/api/admin/bootstrap')
        self.assertEqual(response.status_code, 403)

    def test_bootstrap_shape_and_data(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get('/api/admin/bootstrap')
        self.assertEqual(response.status_code, 200)
        for key in ('approvals', 'venues', 'vendors', 'users', 'bookings',
                    'payouts', 'reviews', 'audit', 'settings'):
            self.assertIn(key, response.data)

        self.assertEqual(len(response.data['venues']), 1)
        self.assertEqual(response.data['venues'][0]['name'], 'Grand Hall')
        self.assertEqual(response.data['venues'][0]['price'], '₹1,200')
        self.assertEqual(len(response.data['vendors']), 1)
        self.assertEqual(len(response.data['users']), 1)
        self.assertEqual(len(response.data['bookings']), 1)
        self.assertEqual(response.data['bookings'][0]['amountNum'], 1220)
        self.assertEqual(response.data['settings']['fee'], '20')

    def test_bootstrap_uses_detail_error_shape_when_denied(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.get('/api/admin/bootstrap')
        self.assertIn('detail', response.data)  # {"detail"}, not {"message"}


class AdminWriteTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9990000004', name='Asha', email='asha@x.in',
        )
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall',
            record={'name': 'Grand Hall', 'price': 1200, 'detail': {}},
            name='Grand Hall', category='hall', locality='HSR', pincode='560102',
        )
        self.draft = VenueDraft.objects.create(
            vendor=self.vendor, status=VenueDraft.Status.PENDING,
            submitted_at=timezone.now(),
        )
        self.booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['19:00 – 20:00'], amount=1220, customer_name='Asha',
            venue_name='Grand Hall',
        )
        self.client.force_authenticate(user=self.admin)

    # --- approvals (keyed by LISTING id) ---
    def test_approve_makes_listing_live(self):
        Listing.objects.filter(pk=self.listing.pk).update(status='pending')
        r = self.client.patch(
            f'/api/admin/approvals/{self.listing.id}', {'status': 'approved'}, format='json'
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'approved')
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, 'live')

    def test_toggle_checklist_and_notes(self):
        r = self.client.patch(
            f'/api/admin/approvals/{self.listing.id}',
            {'checks': {'photos': True}, 'notes': 'Verified on call.'},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['checks']['photos'])
        self.assertEqual(r.data['notes'], 'Verified on call.')

    def test_invalid_approval_status_rejected(self):
        r = self.client.patch(
            f'/api/admin/approvals/{self.listing.id}', {'status': 'weird'}, format='json'
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('detail', r.data)

    # --- venues ---
    def test_pause_and_feature_venue(self):
        paused = self.client.patch(
            f'/api/admin/venues/{self.listing.id}', {'status': 'paused'}, format='json'
        )
        self.assertEqual(paused.data['status'], 'paused')
        featured = self.client.patch(
            f'/api/admin/venues/{self.listing.id}', {'featured': True}, format='json'
        )
        self.assertTrue(featured.data['featured'])

    def test_paused_venue_hidden_from_public(self):
        self.client.patch(
            f'/api/admin/venues/{self.listing.id}', {'status': 'paused'}, format='json'
        )
        self.client.force_authenticate(user=None)
        listed = self.client.get('/api/venues').data
        self.assertEqual(listed['total'], 0)  # paused -> not public

    # --- vendors ---
    def test_verify_kyc_and_suspend(self):
        # Complete the seeded booking so the suspension guard can't 409 —
        # otherwise this test flakes on the time of day it runs.
        Booking.objects.filter(pk=self.booking.pk).update(
            date=today_ist() - datetime.timedelta(days=1)
        )
        kyc = self.client.patch(
            f'/api/admin/vendors/{self.vendor.id}', {'kyc': 'verified'}, format='json'
        )
        self.assertEqual(kyc.data['kyc'], 'verified')
        susp = self.client.patch(
            f'/api/admin/vendors/{self.vendor.id}', {'acc': 'suspended'}, format='json'
        )
        self.assertEqual(susp.data['acc'], 'suspended')
        self.vendor.refresh_from_db()
        self.assertFalse(self.vendor.is_active)

    # --- users ---
    def test_block_user(self):
        r = self.client.patch(
            f'/api/admin/users/{self.customer.id}', {'status': 'blocked'}, format='json'
        )
        self.assertEqual(r.data['status'], 'blocked')
        self.customer.refresh_from_db()
        self.assertFalse(self.customer.is_active)

    # --- bookings ---
    def test_refund_booking(self):
        r = self.client.patch(
            f'/api/admin/bookings/{self.booking.id}', {'status': 'refunded'}, format='json'
        )
        self.assertEqual(r.data['status'], 'refunded')
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.status, 'refunded')

    # --- guards ---
    def test_non_admin_forbidden(self):
        self.client.force_authenticate(user=self.customer)
        r = self.client.patch(
            f'/api/admin/venues/{self.listing.id}', {'status': 'paused'}, format='json'
        )
        self.assertEqual(r.status_code, 403)

    def test_unknown_entity_404_detail_shape(self):
        r = self.client.patch(
            f'/api/admin/venues/{uuid.uuid4()}', {'status': 'paused'}, format='json'
        )
        self.assertEqual(r.status_code, 404)
        self.assertIn('detail', r.data)


class AdminPhase3Tests(APITestCase):
    """Settings, payouts, reviews, audit (new models)."""

    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.client.force_authenticate(user=self.admin)

    # --- settings ---
    def test_settings_put_and_bootstrap(self):
        r = self.client.put(
            '/api/admin/settings',
            {'fee': '30', 'cities': ['HSR']},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['fee'], '30')
        self.assertEqual(r.data['cities'], ['HSR'])
        self.assertNotIn('commission', r.data)  # commission is gone
        boot = self.client.get('/api/admin/bootstrap')
        self.assertEqual(boot.data['settings']['fee'], '30')

    def test_settings_fee_drives_booking_fee(self):
        self.client.put('/api/admin/settings', {'fee': '50'}, format='json')
        vendor = User.objects.create_user(
            phone='9990000003', name='R', email='r@x.in', role=User.Role.VENDOR,
        )
        customer = User.objects.create_user(phone='9990000004', name='A', email='a@x.in')
        listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=vendor, slug='h',
            record={'name': 'H', 'price': 600, 'detail': {}},
            name='H', category='hall', locality='x', pincode='560001',
        )
        self.client.force_authenticate(user=customer)
        tomorrow = (today_ist() + datetime.timedelta(days=1)).isoformat()
        resp = self.client.post('/api/users/me/bookings', {
            'venueId': str(listing.id), 'date': tomorrow,
            'slots': ['19:00 – 20:00'], 'addons': [], 'perSlot': 600,
            'amount': 650,  # 600 (1h) + ₹50 fee
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['booking']['amount'], 650)

    # --- payouts ---
    def test_payout_update(self):
        payout = Payout.objects.create(vendor='Ravi', period='6 – 12 Jul', gross=5000)
        r = self.client.patch(
            f'/api/admin/payouts/{payout.id}', {'status': 'completed'}, format='json'
        )
        self.assertEqual(r.data['status'], 'completed')
        self.assertEqual(r.data['grossNum'], 5000)

    # --- reviews ---
    def test_review_remove(self):
        review = Review.objects.create(venue='Grand Hall', reviewer='X', rating=1, text='spam')
        r = self.client.post(
            f'/api/admin/reviews/{review.id}/resolve',
            {'action': 'remove', 'reason': 'Spam'}, format='json',
        )
        self.assertEqual(r.status_code, 200)
        review.refresh_from_db()
        self.assertEqual(review.status, 'removed')
        boot = self.client.get('/api/admin/bootstrap')
        self.assertEqual(len(boot.data['reviews']), 0)  # removed -> out of the queue

    def test_review_keep_has_stars(self):
        review = Review.objects.create(venue='V', reviewer='X', rating=3, text='ok')
        r = self.client.post(
            f'/api/admin/reviews/{review.id}/resolve', {'action': 'keep'}, format='json'
        )
        self.assertEqual(r.data['stars'], '★★★☆☆')

    def test_bad_review_action_rejected(self):
        review = Review.objects.create(venue='V', reviewer='X', rating=3)
        r = self.client.post(
            f'/api/admin/reviews/{review.id}/resolve', {'action': 'nuke'}, format='json'
        )
        self.assertEqual(r.status_code, 400)

    # --- audit ---
    def test_audit_append_and_bootstrap(self):
        r = self.client.post('/api/admin/audit', {
            'admin': 'Anita', 'action': 'Logged in', 'target': '-', 'change': '',
        }, format='json')
        self.assertEqual(r.status_code, 201)
        boot = self.client.get('/api/admin/bootstrap')
        self.assertTrue(any(a['action'] == 'Logged in' for a in boot.data['audit']))

    def test_server_writes_audit_on_write(self):
        vendor = User.objects.create_user(
            phone='9990000009', name='Ravi', email='rv@x.in', role=User.Role.VENDOR,
        )
        self.client.patch(
            f'/api/admin/vendors/{vendor.id}', {'kyc': 'verified'}, format='json'
        )
        entry = AuditEntry.objects.filter(action='Vendor update').first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.target, 'Ravi')             # name, not id
        self.assertEqual(entry.target_id, str(vendor.id))  # id kept separately


class ConsolidatedRoundTests(APITestCase):
    """New items from the consolidated backlog: refunds free slots + carry
    reason/amount, suspension cascade, walk-in live-only, payout carry-forward,
    enriched admin rows."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9990000004', name='A', email='a@x.in')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='hall',
            record={'name': 'Hall', 'price': 600, 'detail': {}},
            name='Hall', category='hall', locality='x', pincode='560001',
        )
        import datetime as dt
        self.tomorrow = today_ist() + dt.timedelta(days=1)

    def _make_booking(self, **overrides):
        fields = dict(
            listing=self.listing, user=self.customer, date=self.tomorrow,
            slots=['18:00 – 20:00'], amount=1220, fee=20, customer_name='A',
            venue_name='Hall',
        )
        fields.update(overrides)
        return Booking.objects.create(**fields)

    # --- refunds free slots + store reason/amount + online-only ---
    def test_refund_frees_the_slot_and_stores_details(self):
        booking = self._make_booking()
        avail = f'/api/venues/{self.listing.id}/availability?date={self.tomorrow.isoformat()}'
        self.assertEqual(self.client.get(avail).data['booked'], ['18:00 – 20:00'])

        self.client.force_authenticate(user=self.admin)
        r = self.client.patch(
            f'/api/admin/bookings/{booking.id}',
            {'status': 'refunded', 'reason': 'Venue paused (rain)', 'refundAmount': 1220},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['refundReason'], 'Venue paused (rain)')
        self.assertEqual(r.data['refundAmount'], 1220)

        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(avail).data['booked'], [])  # slot freed
        # And the slot is genuinely rebookable:
        self.client.force_authenticate(user=self.customer)
        rebook = self.client.post('/api/users/me/bookings', {
            'venueId': str(self.listing.id), 'date': self.tomorrow.isoformat(),
            'slots': ['18:00 – 20:00'], 'addons': [], 'perSlot': 600, 'amount': 1220,
        }, format='json')
        self.assertEqual(rebook.status_code, 201)

    def test_pay_at_venue_booking_not_refundable(self):
        booking = self._make_booking(method=Booking.Method.VENUE)
        self.client.force_authenticate(user=self.admin)
        r = self.client.patch(
            f'/api/admin/bookings/{booking.id}', {'status': 'refunded'}, format='json'
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn('detail', r.data)

    # --- vendor suspension cascade ---
    def test_suspend_blocked_by_active_booking(self):
        self._make_booking()
        self.client.force_authenticate(user=self.admin)
        r = self.client.patch(
            f'/api/admin/vendors/{self.vendor.id}', {'acc': 'suspended'}, format='json'
        )
        self.assertEqual(r.status_code, 409)
        self.vendor.refresh_from_db()
        self.assertTrue(self.vendor.is_active)  # unchanged

    def test_suspend_after_refund_pauses_venues(self):
        booking = self._make_booking()
        self.client.force_authenticate(user=self.admin)
        self.client.patch(
            f'/api/admin/bookings/{booking.id}', {'status': 'refunded'}, format='json'
        )
        r = self.client.patch(
            f'/api/admin/vendors/{self.vendor.id}', {'acc': 'suspended'}, format='json'
        )
        self.assertEqual(r.status_code, 200)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, 'paused')  # gone from /venues
        # Reactivate: venues STAY paused (manual review before relisting).
        self.client.patch(
            f'/api/admin/vendors/{self.vendor.id}', {'acc': 'active'}, format='json'
        )
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, 'paused')

    # --- walk-ins only on live venues ---
    def test_walkin_rejected_on_pending_venue(self):
        Listing.objects.filter(pk=self.listing.pk).update(status='pending')
        self.client.force_authenticate(user=self.vendor)
        r = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.listing.id), 'date': self.tomorrow.isoformat(),
            'slots': ['10:00 – 11:00'], 'customer': 'W', 'perSlot': 600, 'amount': 600,
        }, format='json')
        self.assertEqual(r.status_code, 400)

    # --- payout carry-forward ---
    def test_negative_week_carries_into_next_payout(self):
        import datetime as dt
        monday = today_ist() - dt.timedelta(days=today_ist().weekday())
        week1, week2 = monday - dt.timedelta(days=14), monday - dt.timedelta(days=7)
        # Week 1: only an at-venue booking -> net −20, no row.
        self._make_booking(date=week1, method=Booking.Method.VENUE, amount=620)
        # Week 2: online 620 -> (620−20) − carried 20 = 580.
        self._make_booking(date=week2, slots=['10:00 – 11:00'], amount=620)
        self.client.force_authenticate(user=self.admin)
        payouts = self.client.get('/api/admin/bootstrap').data['payouts']
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0]['grossNum'], 580)
        self.assertEqual(payouts[0]['periodStart'], week2.isoformat())

    # --- enriched admin rows ---
    def test_admin_booking_rows_carry_phone_and_unit(self):
        self._make_booking(
            phone='9990000004', sport='Box Cricket', unit=2, unit_label='Pitch 2',
        )
        self.client.force_authenticate(user=self.admin)
        row = self.client.get('/api/admin/bootstrap').data['bookings'][0]
        self.assertEqual(row['phone'], '9990000004')
        self.assertEqual(row['sport'], 'Box Cricket')
        self.assertEqual(row['unit'], 2)
        self.assertEqual(row['unitLabel'], 'Pitch 2')

    def test_venue_rows_carry_district_from_draft(self):
        VenueDraft.objects.create(
            vendor=self.vendor, id=self.listing.id,
            data={'location': {'district': 'Warangal', 'city': 'Hanamkonda'}},
        )
        self.client.force_authenticate(user=self.admin)
        venues = self.client.get('/api/admin/bootstrap').data['venues']
        self.assertEqual(venues[0]['district'], 'Warangal')
        self.assertEqual(venues[0]['city'], 'Hanamkonda')


class PublicBannersTests(APITestCase):
    """GET /api/banners — homepage promo banners from admin Settings."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # the endpoint caches 60s — isolate tests
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )

    def save_banners(self, banners):
        self.client.force_authenticate(user=self.admin)
        response = self.client.put(
            '/api/admin/settings', {'banners': banners}, format='json'
        )
        self.client.force_authenticate(user=None)
        return response

    def test_all_banner_fields_round_trip(self):
        banner = {
            'id': 1722400000000, 'title': 'Weekend turf offer',
            'text': 'On all box cricket turfs', 'type': 'percent',
            'value': 15, 'code': 'AUG15', 'minAmount': '500',
            'maxDiscount': '200', 'from': '2026-08-01', 'to': '2026-08-15',
        }
        response = self.save_banners([banner])
        self.assertEqual(response.data['banners'], [banner])  # verbatim echo
        self.client.force_authenticate(user=self.admin)
        boot = self.client.get('/api/admin/bootstrap')
        self.assertEqual(boot.data['settings']['banners'], [banner])

    def test_public_endpoint_filters_by_date_window(self):
        import datetime as dt
        today = today_ist()
        active = {'id': 1, 'title': 'Active', 'text': '', 'type': 'none', 'value': 0,
                  'from': today.isoformat(), 'to': today.isoformat()}
        future = {'id': 2, 'title': 'Future', 'text': '', 'type': 'flat', 'value': 100,
                  'from': (today + dt.timedelta(days=5)).isoformat(), 'to': ''}
        expired = {'id': 3, 'title': 'Expired', 'text': '', 'type': 'percent', 'value': 10,
                   'from': '', 'to': (today - dt.timedelta(days=1)).isoformat()}
        open_ended = {'id': 4, 'title': 'Always', 'text': '', 'type': 'none', 'value': 0,
                      'from': '', 'to': ''}
        untitled = {'id': 5, 'title': '', 'text': 'no title', 'type': 'none', 'value': 0,
                    'from': '', 'to': ''}
        self.save_banners([active, future, expired, open_ended, untitled])

        response = self.client.get('/api/banners')  # public, no auth
        self.assertEqual(response.status_code, 200)
        ids = [b['id'] for b in response.data['banners']]
        self.assertEqual(ids, [1, 4])  # admin order preserved; others filtered

    def test_no_banners_returns_empty_list(self):
        response = self.client.get('/api/banners')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'banners': []})


class PlatformFeeTests(APITestCase):
    """GET /api/config + the fee actually applying (and freezing per booking)."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # /api/config caches 60s — isolate tests
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9990000004', name='A', email='a@x.in')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='hall',
            record={'name': 'Hall', 'price': 600, 'detail': {}},
            name='Hall', category='hall', locality='x', pincode='560001',
        )

    def set_fee(self, fee, fee_date=''):
        from django.core.cache import cache
        self.client.force_authenticate(user=self.admin)
        self.client.put(
            '/api/admin/settings', {'fee': fee, 'feeDate': fee_date}, format='json'
        )
        self.client.force_authenticate(user=None)
        cache.clear()

    def book(self, amount, slot='10:00 – 11:00'):
        import datetime as dt
        self.client.force_authenticate(user=self.customer)
        tomorrow = (today_ist() + dt.timedelta(days=1)).isoformat()
        return self.client.post('/api/users/me/bookings', {
            'venueId': str(self.listing.id), 'date': tomorrow,
            'slots': [slot], 'addons': [], 'perSlot': 600,
            'amount': amount,
        }, format='json')

    def test_config_default(self):
        response = self.client.get('/api/config')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['fee'], 20)

    def test_config_effective_today(self):
        self.set_fee('30', today_ist().isoformat())
        self.assertEqual(self.client.get('/api/config').data['fee'], 30)

    def test_future_feeDate_not_applied_yet(self):
        import datetime as dt
        future = (today_ist() + dt.timedelta(days=5)).isoformat()
        self.set_fee('30', future)
        data = self.client.get('/api/config').data
        self.assertEqual(data['fee'], 20)        # upcoming fee not active yet
        self.assertEqual(data['feeDate'], future)

    def test_acceptance_flow_fee_change_and_freeze(self):
        # 1-2: fee 30 effective today -> /api/config says 30.
        self.set_fee('30', today_ist().isoformat())
        # 3: 1h at 600 -> bill 630, booking succeeds, fee frozen on record.
        response = self.book(630)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['amount'], 630)
        self.assertEqual(response.data['booking']['fee'], 30)
        booking_id = response.data['booking']['id']
        # 4: back to 20 -> new bookings charge 20; the old one keeps fee 30.
        self.set_fee('20')
        second = self.book(620, slot='12:00 – 13:00')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data['booking']['fee'], 20)
        self.assertEqual(Booking.objects.get(pk=booking_id).fee, 30)

    def test_recompute_matches_config_with_future_feeDate(self):
        # Future feeDate: /api/config says 20 AND the recompute charges 20 —
        # both read the same source, no mismatch possible.
        import datetime as dt
        self.set_fee('30', (today_ist() + dt.timedelta(days=5)).isoformat())
        response = self.book(620)  # 600 + default 20
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['booking']['fee'], 20)

    def test_payout_deducts_frozen_fee(self):
        # A booking charged fee 30 last week -> payout deducts 30, not the
        # current setting.
        import datetime as dt
        monday = today_ist() - dt.timedelta(days=today_ist().weekday())
        last_mon = monday - dt.timedelta(days=7)
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=last_mon,
            slots=['10:00 – 11:00'], amount=630, fee=30,
        )
        self.client.force_authenticate(user=self.admin)
        payouts = self.client.get('/api/admin/bootstrap').data['payouts']
        self.assertEqual(payouts[0]['grossNum'], 600)  # 630 − 30


class ApprovalWorkflowTests(APITestCase):
    """Integration round 2, item 1 — the full approve/reject lifecycle."""

    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.base = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='turf',
            record={'name': 'Turf', 'price': 500, 'detail': {}},
            name='Turf', category='Box cricket', locality='HSR', pincode='560102',
            status='pending',
        )
        self.sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='turf-pitch-2',
            record={'name': 'Turf — Pitch 2', 'price': 700,
                    'detail': {'unitOf': str(self.base.id), 'unitLabel': 'Pitch 2'}},
            name='Turf — Pitch 2', category='Box cricket', locality='HSR',
            pincode='560102', status='pending',
        )

    def _clear_cache(self):
        from django.core.cache import cache
        cache.clear()

    def test_pending_venue_hidden_from_public(self):
        self._clear_cache()
        response = self.client.get('/api/venues')
        self.assertEqual(response.data['total'], 0)

    def test_pending_venue_visible_in_vendor_dashboard(self):
        self.client.force_authenticate(user=self.vendor)
        venues = self.client.get('/api/vendors/me/dashboard').data['venues']
        self.assertEqual(len(venues), 2)
        self.assertIn('pending', {v['status'] for v in venues})

    def test_pending_listing_appears_in_approvals(self):
        self.client.force_authenticate(user=self.admin)
        approvals = self.client.get('/api/admin/bootstrap').data['approvals']
        names = [a['name'] for a in approvals]
        self.assertIn('Turf', names)
        self.assertNotIn('Turf — Pitch 2', names)  # siblings never queue
        row = approvals[names.index('Turf')]
        self.assertIn('photos', row)  # gallery grid for the review page

    def test_approve_cascades_to_unit_family(self):
        self.client.force_authenticate(user=self.admin)
        r = self.client.patch(
            f'/api/admin/approvals/{self.base.id}', {'status': 'approved'}, format='json'
        )
        self.assertEqual(r.status_code, 200)
        self.base.refresh_from_db()
        self.sibling.refresh_from_db()
        self.assertEqual(self.base.status, 'live')
        self.assertEqual(self.sibling.status, 'live')   # pitches bookable too
        self._clear_cache()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/venues').data['total'], 2)

    def test_reject_keeps_family_off_catalogue(self):
        self.client.force_authenticate(user=self.admin)
        self.client.patch(
            f'/api/admin/approvals/{self.base.id}', {'status': 'rejected'}, format='json'
        )
        self.sibling.refresh_from_db()
        self.assertEqual(self.sibling.status, 'rejected')
        self._clear_cache()
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get('/api/venues').data['total'], 0)

    def test_new_sibling_of_live_base_goes_live_directly(self):
        self.base.status = 'live'
        self.base.save(update_fields=['status'])
        draft = VenueDraft.objects.create(vendor=self.vendor)
        self.client.force_authenticate(user=self.vendor)
        r = self.client.post('/api/vendors/me/listings', {
            'id': str(draft.id), 'name': 'Turf — Pitch 3', 'price': 800,
            'detail': {'unitOf': str(self.base.id), 'unitLabel': 'Pitch 3'},
        }, format='json')
        self.assertEqual(r.data['listing']['status'], 'live')

    def test_bootstrap_venues_excludes_siblings(self):
        self.client.force_authenticate(user=self.admin)
        venues = self.client.get('/api/admin/bootstrap').data['venues']
        self.assertEqual([v['name'] for v in venues], ['Turf'])

    def test_pause_unpause_cascade_is_symmetric(self):
        # Approve first so the family is live.
        self.client.force_authenticate(user=self.admin)
        self.client.patch(
            f'/api/admin/approvals/{self.base.id}', {'status': 'approved'}, format='json'
        )
        for _ in range(3):  # repeatable: pause → hidden, unpause → back
            r = self.client.patch(
                f'/api/admin/venues/{self.base.id}', {'status': 'paused'}, format='json'
            )
            self.assertEqual(r.status_code, 200)
            self.sibling.refresh_from_db()
            self.assertEqual(self.sibling.status, 'paused')  # family hidden too
            self._clear_cache()
            self.assertEqual(self.client.get('/api/venues').data['total'], 0)

            r = self.client.patch(
                f'/api/admin/venues/{self.base.id}', {'status': 'live'}, format='json'
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data['status'], 'live')  # echoes updated entity
            self.sibling.refresh_from_db()
            self.assertEqual(self.sibling.status, 'live')    # family restored
            self._clear_cache()
            self.assertEqual(self.client.get('/api/venues').data['total'], 2)


class CustomerVendorIdentityTests(APITestCase):
    """Integration round 2, item 2 — one phone can be customer AND vendor."""

    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )

    def test_vendor_who_books_appears_in_both_lists(self):
        vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=vendor, slug='hall',
            record={'name': 'Hall', 'price': 600, 'detail': {}},
            name='Hall', category='hall', locality='x', pincode='560001',
        )
        # The vendor books a venue as a customer:
        self.client.force_authenticate(user=vendor)
        import datetime as dt
        tomorrow = (today_ist() + dt.timedelta(days=1)).isoformat()
        r = self.client.post('/api/users/me/bookings', {
            'venueId': str(listing.id), 'date': tomorrow,
            'slots': ['10:00 – 11:00'], 'addons': [], 'perSlot': 600, 'amount': 620,
        }, format='json')
        self.assertEqual(r.status_code, 201)

        self.client.force_authenticate(user=self.admin)
        boot = self.client.get('/api/admin/bootstrap').data
        vendor_phones = {v['phone'] for v in boot['vendors']}
        user_phones = {u['phone'] for u in boot['users']}
        self.assertIn('9990000003', vendor_phones)
        self.assertIn('9990000003', user_phones)  # BOTH lists

    def test_customer_otp_login_sets_customer_flag(self):
        vendor = User.objects.create_user(
            phone='9990000004', name='V', email='v2@x.in', role=User.Role.VENDOR,
        )
        with patch('accounts.views.send_otp_sms', side_effect=lambda p, c: None):
            self.client.post('/api/users/auth/otp', {'phone': '9990000004'}, format='json')
        from accounts.models import PhoneOTP
        from django.contrib.auth.hashers import make_password
        PhoneOTP.objects.filter(phone='9990000004').update(code_hash=make_password('123456'))
        self.client.post(
            '/api/users/auth/verify',
            {'phone': '9990000004', 'otp': '123456'}, format='json',
        )
        vendor.refresh_from_db()
        self.assertTrue(vendor.is_customer)
        self.assertEqual(vendor.role, User.Role.VENDOR)  # vendor role untouched


class PayoutGenerationTests(APITestCase):
    """Integration round 2, item 4 — weekly payout rows from online bookings."""

    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000001', name='Anita', email=ADMIN_EMAIL,
            role=User.Role.ADMIN, password=ADMIN_PASSWORD,
        )
        self.vendor = User.objects.create_user(
            phone='9990000003', name='Ravi', email='ravi@x.in', role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(phone='9990000004', name='A', email='a@x.in')
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='hall',
            record={'name': 'Hall', 'price': 600, 'detail': {}},
            name='Hall', category='hall', locality='x', pincode='560001',
        )
        # Last week (fully completed): Monday of this week minus 7/6 days.
        import datetime as dt
        monday = today_ist() - dt.timedelta(days=today_ist().weekday())
        self.last_mon = monday - dt.timedelta(days=7)
        self.last_tue = self.last_mon + dt.timedelta(days=1)

    def _boot(self):
        self.client.force_authenticate(user=self.admin)
        return self.client.get('/api/admin/bootstrap').data

    def test_online_bookings_minus_fee(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=620, customer_name='A',
        )
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_tue,
            slots=['12:00 – 13:00'], amount=1220, customer_name='A',
        )
        payouts = self._boot()['payouts']
        self.assertEqual(len(payouts), 1)
        row = payouts[0]
        self.assertEqual(row['vendor'], 'Ravi')
        self.assertEqual(row['grossNum'], (620 - 20) + (1220 - 20))
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['periodStart'], self.last_mon.isoformat())

    def test_walkins_and_refunds_excluded(self):
        Booking.objects.create(   # walk-in — vendor already has the cash
            listing=self.listing, user=None, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=600,
            method=Booking.Method.WALK_IN, walk_in=True,
        )
        Booking.objects.create(   # refunded — no payout
            listing=self.listing, user=self.customer, date=self.last_tue,
            slots=['12:00 – 13:00'], amount=1220, status='refunded',
        )
        self.assertEqual(self._boot()['payouts'], [])

    def test_pay_at_venue_deducts_only_the_fee(self):
        # Frontend example: online bookings pass through minus their fees,
        # at-venue bookings contribute MINUS their fee only (vendor holds the
        # cash). 1220+620 online (fee 20 each) + one at-venue (fee 20):
        # (1220-20) + (620-20) - 20 = 1780.
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=1220, fee=20,
        )
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['12:00 – 13:00'], amount=620, fee=20,
        )
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_tue,
            slots=['14:00 – 15:00'], amount=620, fee=20,
            method=Booking.Method.VENUE,
        )
        payouts = self._boot()['payouts']
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0]['grossNum'], 1200 + 600 - 20)

    def test_platform_promo_topped_up_venue_offer_not(self):
        # Platform promo: base 2000, discount 300 -> amount 1720 (with ₹20
        # fee). Vendor is made whole: 1720 + 300 - 20 = 2000.
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=1720, fee=20, discount_amount=300,
            offer={'code': 'AUG15', 'source': 'platform'},
        )
        # Venue offer, same numbers: vendor funds it -> 1720 - 20 = 1700.
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_tue,
            slots=['12:00 – 13:00'], amount=1720, fee=20, discount_amount=300,
            offer={'code': 'SAVE10', 'source': 'venue'},
        )
        payouts = self._boot()['payouts']
        self.assertEqual(payouts[0]['grossNum'], 2000 + 1700)

    def test_upi_counts_as_online_paid_in_payouts(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=620, fee=20,
            method=Booking.Method.UPI,
        )
        payouts = self._boot()['payouts']
        self.assertEqual(payouts[0]['grossNum'], 600)  # amount − fee, passed through

    def test_generation_is_idempotent_and_keeps_admin_status(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=620,
        )
        payout_id = self._boot()['payouts'][0]['id']
        self.client.patch(
            f'/api/admin/payouts/{payout_id}', {'status': 'completed'}, format='json'
        )
        payouts = self._boot()['payouts']  # regenerating must not duplicate/reset
        self.assertEqual(len(payouts), 1)
        self.assertEqual(payouts[0]['status'], 'completed')

    def test_booking_rows_have_iso_date(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=self.last_mon,
            slots=['10:00 – 11:00'], amount=620,
        )
        bookings = self._boot()['bookings']
        self.assertEqual(bookings[0]['date'], self.last_mon.isoformat())
        self.assertIn('createdAt', bookings[0])
        self.assertEqual(bookings[0]['slots'], ['10:00 – 11:00'])  # raw slots too

    def test_todays_booking_completes_after_last_slot_ends(self):
        Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist(),
            slots=['06:00 – 08:00'], amount=620,
        )
        # 07:00 IST -> still running; 09:00 IST -> completed.
        with patch('adminpanel.formatters.now_minutes_ist', return_value=7 * 60):
            self.assertEqual(self._boot()['bookings'][0]['status'], 'confirmed')
        with patch('adminpanel.formatters.now_minutes_ist', return_value=9 * 60):
            self.assertEqual(self._boot()['bookings'][0]['status'], 'completed')
