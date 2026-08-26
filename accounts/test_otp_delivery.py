"""
OTP delivery — one code, many channels.

Fast2SMS and Resend do not generate anything; they are delivery pipes for a
code this server generated. These tests pin that: whatever else changes, the
number in the SMS and the number in the email must be identical.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from .otp import OTPSendError, deliver_otp


@override_settings(
    FAST2SMS_API_KEY='fake-sms-key',
    RESEND_API_KEY='fake-resend-key',
    RESEND_FROM='noreply@example.com',
)
class DeliverOtpTests(TestCase):
    def test_sms_and_email_carry_the_same_code(self):
        with patch('accounts.otp.send_otp_sms') as sms, \
             patch('accounts.otp.send_otp_email') as email:
            deliver_otp('483920', '9000000001', 'asha@example.com')

        sms.assert_called_once_with('9000000001', '483920')
        email.assert_called_once_with('asha@example.com', '483920')
        # The actual assertion that matters:
        self.assertEqual(sms.call_args[0][1], email.call_args[0][1])

    def test_no_email_on_file_is_sms_only(self):
        with patch('accounts.otp.send_otp_sms') as sms, \
             patch('accounts.otp.send_otp_email') as email:
            deliver_otp('483920', '9000000001', '')

        sms.assert_called_once()
        email.assert_not_called()

    def test_email_failure_does_not_block_login(self):
        """One channel down must not lock the user out."""
        with patch('accounts.otp.send_otp_sms') as sms, \
             patch('accounts.otp.send_otp_email',
                   side_effect=OTPSendError('resend down')):
            deliver_otp('483920', '9000000001', 'asha@example.com')
        sms.assert_called_once()

    def test_sms_failure_still_delivers_by_email(self):
        with patch('accounts.otp.send_otp_sms',
                   side_effect=OTPSendError('fast2sms down')), \
             patch('accounts.otp.send_otp_email') as email:
            deliver_otp('483920', '9000000001', 'asha@example.com')
        email.assert_called_once()

    def test_a_channel_crashing_does_not_break_login(self):
        """REGRESSION: the email path raised something that was not an
        OTPSendError, it escaped deliver_otp, and login 500'd — even though
        the SMS had already gone out. The user got a code the server never
        stored."""
        with patch('accounts.otp.send_otp_sms') as sms, \
             patch('accounts.otp.send_otp_email',
                   side_effect=RuntimeError('template blew up')):
            deliver_otp('483920', '9000000001', 'asha@example.com')  # must not raise
        sms.assert_called_once()

    def test_sms_crashing_still_delivers_by_email(self):
        with patch('accounts.otp.send_otp_sms',
                   side_effect=RuntimeError('provider exploded')), \
             patch('accounts.otp.send_otp_email') as email:
            deliver_otp('483920', '9000000001', 'asha@example.com')
        email.assert_called_once()

    def test_all_channels_failing_raises(self):
        """Never store an OTP the user could not possibly have received."""
        with patch('accounts.otp.send_otp_sms',
                   side_effect=OTPSendError('sms down')), \
             patch('accounts.otp.send_otp_email',
                   side_effect=OTPSendError('email down')):
            with self.assertRaises(OTPSendError):
                deliver_otp('483920', '9000000001', 'asha@example.com')

    def test_code_never_appears_in_the_request_url(self):
        """Resend gets the code in the JSON body, never in a logged URL."""
        captured = {}

        class FakeResponse:
            status_code = 200

        def fake_post(url, **kwargs):
            captured['url'] = url
            captured['json'] = kwargs.get('json')
            return FakeResponse()

        with patch('accounts.otp.requests.post', side_effect=fake_post):
            from .otp import send_otp_email
            send_otp_email('asha@example.com', '483920')

        self.assertNotIn('483920', captured['url'])
        self.assertIn('483920', captured['json']['text'])
