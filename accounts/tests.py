"""
Tests for the OTP auth flow.

The 2Factor SMS call is MOCKED everywhere — tests never send real SMS
(no balance used) and the OTP code is captured in-memory, not printed.
"""
import datetime
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import PhoneOTP, User

PHONE = '9876543210'
OTHER_PHONE = '9123456780'


class OTPAuthTestBase(APITestCase):
    def setUp(self):
        # Throttle counters live in the cache — reset between tests.
        cache.clear()
        self.sent = {}  # phone -> last code "sent"

        def fake_send(phone, code):
            self.sent[phone] = code

        patcher = patch('accounts.views.send_otp_sms', side_effect=fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request_otp(self, url, phone=PHONE):
        return self.client.post(url, {'phone': phone}, format='json')

    def verify(self, url, phone=PHONE, otp=None):
        return self.client.post(
            url, {'phone': phone, 'otp': otp or self.sent[phone]}, format='json'
        )


class UserOTPTests(OTPAuthTestBase):
    OTP_URL = '/api/users/auth/otp'
    VERIFY_URL = '/api/users/auth/verify'

    def test_request_otp_returns_sent_to(self):
        response = self.request_otp(self.OTP_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'sentTo': PHONE})
        self.assertIn(PHONE, self.sent)

    def test_invalid_phone_rejected(self):
        response = self.request_otp(self.OTP_URL, phone='12345')
        self.assertEqual(response.status_code, 400)
        self.assertIn('message', response.data)

    def test_verify_creates_customer_and_returns_token(self):
        self.request_otp(self.OTP_URL)
        response = self.verify(self.VERIFY_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn('token', response.data)
        self.assertEqual(response.data['user']['phone'], PHONE)
        user = User.objects.get(phone=PHONE)
        self.assertEqual(user.role, User.Role.PUBLIC)

    def test_wrong_otp_rejected_and_attempt_counted(self):
        self.request_otp(self.OTP_URL)
        if self.sent[PHONE] == '000000':  # astronomically unlikely collision
            return
        response = self.verify(self.VERIFY_URL, otp='000000')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(PhoneOTP.objects.get(phone=PHONE).attempts, 1)

    def test_otp_is_single_use(self):
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        response = self.verify(self.VERIFY_URL)  # second time, same code
        self.assertEqual(response.status_code, 401)

    def test_expired_otp_rejected(self):
        self.request_otp(self.OTP_URL)
        PhoneOTP.objects.filter(phone=PHONE).update(
            expires_at=timezone.now() - datetime.timedelta(minutes=1)
        )
        response = self.verify(self.VERIFY_URL)
        self.assertEqual(response.status_code, 401)

    def test_attempts_limit_locks_otp(self):
        self.request_otp(self.OTP_URL)
        PhoneOTP.objects.filter(phone=PHONE).update(attempts=PhoneOTP.MAX_ATTEMPTS)
        response = self.verify(self.VERIFY_URL)
        self.assertEqual(response.status_code, 429)

    def test_per_phone_request_limit(self):
        for _ in range(3):
            self.assertEqual(self.request_otp(self.OTP_URL).status_code, 200)
        response = self.request_otp(self.OTP_URL)
        self.assertEqual(response.status_code, 429)

    def test_sms_failure_returns_502_and_stores_nothing(self):
        from accounts.otp import OTPSendError
        with patch('accounts.views.send_otp_sms', side_effect=OTPSendError('down')):
            response = self.request_otp(self.OTP_URL)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(PhoneOTP.objects.count(), 0)

    def test_new_otp_invalidates_old_one(self):
        self.request_otp(self.OTP_URL)
        first_code = self.sent[PHONE]
        self.request_otp(self.OTP_URL)
        if first_code == self.sent[PHONE]:
            return  # same random code twice — cannot distinguish, skip
        response = self.verify(self.VERIFY_URL, otp=first_code)
        self.assertEqual(response.status_code, 401)


class VendorOTPTests(OTPAuthTestBase):
    OTP_URL = '/api/vendors/auth/otp'
    VERIFY_URL = '/api/vendors/auth/verify'
    REGISTER_URL = '/api/vendors'

    def register(self, phone=PHONE, name='Ravi Sharma', email='ravi@example.com'):
        return self.client.post(
            self.REGISTER_URL,
            {'phone': phone, 'name': name, 'email': email},
            format='json',
        )

    def test_unknown_phone_gets_is_new_true_and_no_token(self):
        self.request_otp(self.OTP_URL)
        response = self.verify(self.VERIFY_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {'vendor': None, 'isNew': True})

    def test_register_after_verify_creates_vendor(self):
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertIn('token', response.data)
        self.assertEqual(response.data['vendor']['name'], 'Ravi Sharma')
        self.assertEqual(User.objects.get(phone=PHONE).role, User.Role.VENDOR)

    def test_register_without_verify_forbidden(self):
        response = self.register()
        self.assertEqual(response.status_code, 403)

    def test_returning_vendor_logs_straight_in(self):
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        self.register()

        self.request_otp(self.OTP_URL)
        response = self.verify(self.VERIFY_URL)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['isNew'])
        self.assertIn('token', response.data)
        self.assertEqual(response.data['vendor']['phone'], PHONE)

    def test_register_requires_name(self):
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        response = self.client.post(
            self.REGISTER_URL, {'phone': PHONE, 'name': ''}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_customer_upgraded_to_vendor(self):
        # Same phone first becomes a customer...
        self.request_otp('/api/users/auth/otp')
        self.verify('/api/users/auth/verify')
        # ...then registers as a vendor.
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        response = self.register()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(User.objects.filter(phone=PHONE).count(), 1)  # same account
        self.assertEqual(User.objects.get(phone=PHONE).role, User.Role.VENDOR)

    def test_user_otp_cannot_be_used_for_vendor_register(self):
        # Verify via the CUSTOMER endpoint only...
        self.request_otp('/api/users/auth/otp')
        self.verify('/api/users/auth/verify')
        # ...then try to register as vendor without a vendor OTP.
        response = self.register()
        self.assertEqual(response.status_code, 403)

    def test_duplicate_email_rejected(self):
        User.objects.create_user(
            phone=OTHER_PHONE, name='Existing', email='ravi@example.com'
        )
        self.request_otp(self.OTP_URL)
        self.verify(self.VERIFY_URL)
        response = self.register(email='ravi@example.com')
        self.assertEqual(response.status_code, 400)


class RemovedPasswordEndpointsTests(APITestCase):
    def test_password_login_gone(self):
        response = self.client.post(
            '/api/v1/auth/login', {'phone': PHONE, 'password': 'x'}, format='json'
        )
        self.assertEqual(response.status_code, 404)

    def test_password_register_gone(self):
        response = self.client.post('/api/v1/auth/vendor/register', {}, format='json')
        self.assertEqual(response.status_code, 404)
