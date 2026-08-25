"""
Booking notifications — who gets told, and exactly once.

The rule that matters most: a notification failure must NEVER break a paid
booking. Everything here is best-effort by design.
"""
import datetime
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings

from accounts.models import User
from venues.models import Listing

from .models import Booking
from .notifications import notify_booking_confirmed
from .slots import today_ist
from .tests import RECORD


@override_settings(
    NOTIFY_EMAIL_ENABLED=True, NOTIFY_SMS_ENABLED=False,
    RESEND_API_KEY='fake-key', RESEND_FROM='noreply@example.com',
)
class BookingNotificationTests(TestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9100000001', name='Vendor', email='vendor@example.com',
            role=User.Role.VENDOR,
        )
        self.customer = User.objects.create_user(
            phone='9100000002', name='Asha', email='asha@example.com',
        )
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='n-hall',
            record={**RECORD, 'id': 'n', 'status': 'live'},
            name='Grand Palace Hall', category='hall',
            locality='x', pincode='560001',
        )

    def _booking(self, **overrides):
        fields = dict(
            listing=self.listing, user=self.customer,
            venue_name='Grand Palace Hall', category='hall', location='x',
            image='', customer_name='Asha', phone=self.customer.phone,
            date=today_ist() + datetime.timedelta(days=1),
            slots=['19:30 – 21:00'], per_slot=600, addons=[],
            fee=20, amount=920, method='upi', status='confirmed',
        )
        fields.update(overrides)
        return Booking.objects.create(**fields)

    def test_both_customer_and_vendor_are_emailed(self):
        booking = self._booking()
        with patch('bookings.notifications._send_email') as email:
            self.assertTrue(notify_booking_confirmed(booking))

        recipients = {call.args[0] for call in email.call_args_list}
        self.assertIn('asha@example.com', recipients)     # customer
        self.assertIn('vendor@example.com', recipients)   # vendor

    def test_sms_is_off_by_default_because_it_costs_more_than_the_fee(self):
        booking = self._booking()
        with patch('bookings.notifications._send_email'), \
             patch('bookings.notifications.send_otp_sms') as sms:
            notify_booking_confirmed(booking)
        sms.assert_not_called()

    @override_settings(NOTIFY_SMS_ENABLED=True)
    def test_sms_goes_out_when_explicitly_enabled(self):
        booking = self._booking()
        with patch('bookings.notifications._send_email'), \
             patch('bookings.notifications.send_otp_sms') as sms:
            notify_booking_confirmed(booking)
        numbers = {call.args[0] for call in sms.call_args_list}
        self.assertIn('9100000002', numbers)   # customer
        self.assertIn('9100000001', numbers)   # vendor

    def test_a_failing_provider_never_raises(self):
        """A dead email provider must not take a paid booking down with it."""
        booking = self._booking()
        with patch('bookings.notifications._send_email',
                   side_effect=RuntimeError('resend down')):
            self.assertFalse(notify_booking_confirmed(booking))  # no exception

    def test_confirmation_is_sent_once_even_if_the_webhook_repeats(self):
        from .payments import _confirm_booking
        booking = self._booking(status='payment_pending')
        with patch('bookings.notifications._send_email') as email:
            _confirm_booking(booking, 'pay_1')
            first = email.call_count
            _confirm_booking(booking, 'pay_1')   # Razorpay re-delivers
        self.assertEqual(email.call_count, first)
        booking.refresh_from_db()
        self.assertIsNotNone(booking.confirmation_sent_at)
