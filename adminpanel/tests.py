"""
Tests for the super-admin panel (Phase 1: auth + bootstrap).

The 2Factor SMS call is MOCKED — no real SMS, and the code is captured
in-memory (never printed).
"""
import uuid
from unittest.mock import patch

from rest_framework.test import APITestCase

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from venues.models import Listing

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
