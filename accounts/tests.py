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


class ProfileUpdateTests(APITestCase):
    """PATCH /users/me and /vendors/me — contract P4."""

    def setUp(self):
        self.customer = User.objects.create_user(
            phone='9800000001', name='Old Name', email='old@example.com',
        )
        self.vendor = User.objects.create_user(
            phone='9800000002', name='Vendor', email='vendor@example.com',
            role=User.Role.VENDOR,
        )

    def test_customer_updates_name_and_email(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(
            '/api/users/me',
            {'name': 'New Name', 'email': 'new@example.com'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['name'], 'New Name')
        self.assertEqual(response.data['user']['email'], 'new@example.com')
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.name, 'New Name')
        self.assertEqual(self.customer.email, 'new@example.com')

    def test_phone_is_immutable(self):
        self.client.force_authenticate(user=self.customer)
        self.client.patch(
            '/api/users/me', {'phone': '9999999999', 'name': 'X'}, format='json'
        )
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.phone, '9800000001')  # unchanged

    def test_invalid_email_rejected(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(
            '/api/users/me', {'email': 'not-an-email'}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_email_rejected(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch(
            '/api/users/me', {'email': 'vendor@example.com'}, format='json'
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_name_rejected(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch('/api/users/me', {'name': '   '}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_requires_auth(self):
        response = self.client.patch('/api/users/me', {'name': 'X'}, format='json')
        self.assertEqual(response.status_code, 401)

    def test_vendor_updates_profile(self):
        self.client.force_authenticate(user=self.vendor)
        response = self.client.patch(
            '/api/vendors/me', {'name': 'Ravi Sharma'}, format='json'
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['vendor']['name'], 'Ravi Sharma')
        self.assertEqual(response.data['vendor']['phone'], '9800000002')

    def test_customer_cannot_use_vendor_profile(self):
        self.client.force_authenticate(user=self.customer)
        response = self.client.patch('/api/vendors/me', {'name': 'X'}, format='json')
        self.assertEqual(response.status_code, 403)


class OneTimeProfileFlowTests(OTPAuthTestBase):
    """The frontend's one-time name+email screen — full acceptance flow:
    fresh verify -> empty profile -> PATCH saves -> every later verify
    (any device) echoes the saved values -> admin Users row shows them."""

    OTP_URL = '/api/users/auth/otp'
    VERIFY_URL = '/api/users/auth/verify'

    def test_full_profile_cycle(self):
        # 1. Brand-new phone: verify returns an empty profile + a token.
        self.request_otp(self.OTP_URL)
        first = self.verify(self.VERIFY_URL)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['user']['name'], '')
        self.assertFalse(first.data['user']['email'])  # empty/null -> ask once

        # 2. Save the profile with the verify-issued token (same session).
        token = first.data['token']
        response = self.client.patch(
            '/api/users/me',
            {'name': 'Ravi Kumar', 'email': 'ravi@x.in'},
            format='json', HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['name'], 'Ravi Kumar')

        # 3. Sign in again ("different device") -> profile echoed, no screen.
        self.request_otp(self.OTP_URL)
        second = self.verify(self.VERIFY_URL)
        self.assertEqual(second.data['user']['name'], 'Ravi Kumar')
        self.assertEqual(second.data['user']['email'], 'ravi@x.in')

        # 4. Admin Users row carries the same name AND email.
        admin = User.objects.create_user(
            phone='9990000099', name='Admin', email='ad@x.in',
            role=User.Role.ADMIN, password='AdminPass123',
        )
        self.client.force_authenticate(user=admin)
        users = self.client.get('/api/admin/bootstrap').data['users']
        row = next(u for u in users if u['phone'] == PHONE)
        self.assertEqual(row['name'], 'Ravi Kumar')
        self.assertEqual(row['email'], 'ravi@x.in')

    def test_phone_in_patch_body_is_never_applied(self):
        self.request_otp(self.OTP_URL)
        token = self.verify(self.VERIFY_URL).data['token']
        self.client.patch(
            '/api/users/me',
            {'phone': '1111111111', 'name': 'X'},
            format='json', HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertTrue(User.objects.filter(phone=PHONE).exists())
        self.assertFalse(User.objects.filter(phone='1111111111').exists())


class SmsProviderTests(APITestCase):
    """Which provider sends the OTP, and how failures surface.
    No real SMS is ever sent — the HTTP call is mocked."""

    def _ok(self, payload):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=200)
        response.json.return_value = payload
        return response

    def test_fast2sms_preferred_when_configured(self):
        from django.test import override_settings

        from accounts.otp import send_otp_sms
        with override_settings(
            FAST2SMS_API_KEY='fast-key', TWOFACTOR_API_KEY='two-key'
        ):
            with patch(
                'accounts.otp.requests.get', return_value=self._ok({'return': True})
            ) as mock_get:
                send_otp_sms('9876543210', '123456')
        url = mock_get.call_args[0][0]
        self.assertIn('fast2sms', url)                     # not 2Factor
        kwargs = mock_get.call_args[1]
        self.assertEqual(kwargs['headers']['authorization'], 'fast-key')
        self.assertEqual(kwargs['params']['numbers'], '9876543210')
        self.assertEqual(kwargs['params']['route'], 'otp')

    def test_falls_back_to_2factor_without_fast2sms(self):
        from django.test import override_settings

        from accounts.otp import send_otp_sms
        with override_settings(FAST2SMS_API_KEY='', TWOFACTOR_API_KEY='two-key'):
            with patch(
                'accounts.otp.requests.get',
                return_value=self._ok({'Status': 'Success'}),
            ) as mock_get:
                send_otp_sms('9876543210', '123456')
        self.assertIn('2factor.in', mock_get.call_args[0][0])

    def test_fast2sms_failure_raises(self):
        from django.test import override_settings

        from accounts.otp import OTPSendError, send_otp_sms
        with override_settings(FAST2SMS_API_KEY='fast-key'):
            with patch(
                'accounts.otp.requests.get',
                return_value=self._ok({'return': False, 'message': ['Invalid key']}),
            ):
                with self.assertRaises(OTPSendError):
                    send_otp_sms('9876543210', '123456')

    def test_no_provider_configured_raises(self):
        from django.test import override_settings

        from accounts.otp import OTPSendError, send_otp_sms
        with override_settings(FAST2SMS_API_KEY='', TWOFACTOR_API_KEY=''):
            with self.assertRaises(OTPSendError):
                send_otp_sms('9876543210', '123456')


class BlockedUserTests(OTPAuthTestBase):
    """Admin-blocked accounts must be locked out of OTP sign-in entirely."""

    def setUp(self):
        super().setUp()
        User.objects.create_user(phone=PHONE, name='Bad Actor', is_customer=True)
        User.objects.filter(phone=PHONE).update(is_active=False)

    def test_blocked_phone_cannot_request_otp(self):
        response = self.request_otp('/api/users/auth/otp')
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(PHONE, self.sent)  # no SMS was sent

    def test_blocked_phone_cannot_verify(self):
        # Even if an OTP existed from before the block, verify refuses.
        User.objects.filter(phone=PHONE).update(is_active=True)
        self.request_otp('/api/users/auth/otp')
        User.objects.filter(phone=PHONE).update(is_active=False)
        response = self.verify('/api/users/auth/verify')
        self.assertEqual(response.status_code, 403)

    def test_blocked_vendor_cannot_verify(self):
        response = self.client.post(
            '/api/vendors/auth/otp', {'phone': PHONE}, format='json'
        )
        self.assertEqual(response.status_code, 403)


class RemovedPasswordEndpointsTests(APITestCase):
    def test_password_login_gone(self):
        response = self.client.post(
            '/api/v1/auth/login', {'phone': PHONE, 'password': 'x'}, format='json'
        )
        self.assertEqual(response.status_code, 404)

    def test_password_register_gone(self):
        response = self.client.post('/api/v1/auth/vendor/register', {}, format='json')
        self.assertEqual(response.status_code, 404)
