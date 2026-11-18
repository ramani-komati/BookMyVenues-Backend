"""
QA audit 2026-10-30: the vendor phone field accepted "12345abc12345" — the
letters were stripped and an OTP was requested for 1234512345. Sign-in phone
numbers are now normalised (+91 / 0 prefixes, spaces, hyphens) and anything
else is refused BEFORE an OTP is created or sent.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APITestCase

from .models import PhoneOTP
from .serializers import normalize_mobile


class NormalizeMobileTests(SimpleTestCase):
    def test_accepted_forms(self):
        for raw in ('9876543210', '+91 98765 43210', '+919876543210', '919876543210',
                    '09876543210', '98765-43210', '(987) 654-3210', ' 9876543210 '):
            self.assertEqual(normalize_mobile(raw), '9876543210', raw)

    def test_refused(self):
        for raw in ('12345abc12345', '98765abc10', '1234567890', '5876543210', '987654321',
                    '98765432101', '+1 9876543210', '', None, '９８７６５４３２１０', 'abc'):
            self.assertIsNone(normalize_mobile(raw), raw)


class OtpRequestValidationTests(APITestCase):
    def setUp(self):
        cache.clear()

    def test_invalid_phone_never_creates_or_sends_an_otp(self):
        for url in ('/api/vendors/auth/otp', '/api/users/auth/otp'):
            with patch('accounts.views.deliver_otp') as deliver:
                response = self.client.post(url, {'phone': '12345abc12345'}, format='json')
            self.assertEqual(response.status_code, 400, url)
            self.assertIn('mobile number', str(response.data))
            deliver.assert_not_called()
        self.assertFalse(PhoneOTP.objects.exists())

    def test_country_code_is_normalised(self):
        with patch('accounts.views.deliver_otp'):
            response = self.client.post('/api/vendors/auth/otp', {'phone': '+91 98765 43210'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['sentTo'], '9876543210')
        self.assertTrue(PhoneOTP.objects.filter(phone='9876543210').exists())


class ErrorShapeTests(APITestCase):
    """QA audit 2026-10-30: one error format — a readable sentence under
    "message" (customer/vendor API), field details under "errors"."""

    def test_field_errors_carry_a_message(self):
        from types import SimpleNamespace

        from rest_framework.exceptions import ValidationError

        from .exceptions import api_exception_handler
        error = ValidationError({'phone': ['Enter a valid mobile number.']})
        public = api_exception_handler(error, {'request': SimpleNamespace(path='/api/users/me')})
        self.assertEqual(public.data['message'], 'Enter a valid mobile number.')
        self.assertIn('phone', public.data['errors'])
        self.assertEqual(public.data['code'], 'invalid')
        admin = api_exception_handler(error, {'request': SimpleNamespace(path='/api/admin/x')})
        self.assertEqual(admin.data['detail'], 'Enter a valid mobile number.')
        self.assertNotIn('message', admin.data)
        # The OTP view's own message is already in the same shape.
        response = self.client.post('/api/users/auth/otp', {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(response.data['message'])

    def test_auth_errors_keep_the_message_key(self):
        response = self.client.get('/api/users/me/favorites')
        self.assertEqual(response.status_code, 401)
        self.assertIn('message', response.data)
        self.assertNotIn('detail', response.data)
