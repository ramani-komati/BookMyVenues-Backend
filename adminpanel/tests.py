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

    # --- approvals ---
    def test_approve(self):
        r = self.client.patch(
            f'/api/admin/approvals/{self.draft.id}', {'status': 'approved'}, format='json'
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'approved')
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.review_status, 'approved')

    def test_toggle_checklist_and_notes(self):
        r = self.client.patch(
            f'/api/admin/approvals/{self.draft.id}',
            {'checks': {'photos': True}, 'notes': 'Verified on call.'},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['checks']['photos'])
        self.assertEqual(r.data['notes'], 'Verified on call.')

    def test_invalid_approval_status_rejected(self):
        r = self.client.patch(
            f'/api/admin/approvals/{self.draft.id}', {'status': 'weird'}, format='json'
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
            {'fee': '30', 'commission': '12', 'cities': ['HSR']},
            format='json',
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['fee'], '30')
        self.assertEqual(r.data['commission'], '12')
        self.assertEqual(r.data['cities'], ['HSR'])
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
        self.assertTrue(AuditEntry.objects.filter(action='Vendor update').exists())
