"""
Vendor settings (accounts/vendor_settings.py): business contact details and
booking-alert preferences — and that the alerts actually follow them.
"""
import datetime
import uuid
from unittest import mock

from rest_framework.test import APITestCase

from bookings.models import Booking
from bookings.slots import today_ist
from venues.models import Listing

from .models import User, VendorSettings


class VendorSettingsTests(APITestCase):
    def setUp(self):
        self.vendor = User.objects.create_user(
            phone='9000000501', name='Vendor', email='vendor@example.com', role=User.Role.VENDOR,
        )
        self.client.force_authenticate(user=self.vendor)

    def test_defaults_are_every_alert_on_to_the_account_contact(self):
        data = self.client.get('/api/vendors/me/settings').data
        self.assertEqual(data['notifications'],
                         {'bookingEmail': True, 'bookingSms': True, 'ticketEmail': True, 'ticketSms': True})
        self.assertEqual(data['profile']['phone'], '9000000501')
        self.assertTrue(data['account']['active'])
        self.assertEqual(data['account']['kyc'], 'pending')
        self.assertFalse(VendorSettings.objects.exists())     # reading creates nothing

    def test_update_business_and_notifications(self):
        response = self.client.patch('/api/vendors/me/settings', {
            'business': {'name': 'Arena Sports LLP', 'phone': '9876543210',
                         'email': 'desk@arena.example', 'address': 'Hanamkonda'},
            'notifications': {'bookingSms': False},
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['business']['name'], 'Arena Sports LLP')
        self.assertFalse(response.data['notifications']['bookingSms'])
        self.assertTrue(response.data['notifications']['bookingEmail'])

    def test_invalid_input_is_refused(self):
        for body in (
            {'business': {'phone': '12345'}},
            {'business': {'email': 'not-an-email'}},
            {'business': {'name': 'x' * 151}},
            {'notifications': {'bookingSms': 'no'}},
            {'business': 'Arena'},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch('/api/vendors/me/settings', body, format='json').status_code, 400)

    def test_protected_fields_cannot_be_changed_here(self):
        self.client.patch('/api/vendors/me/settings', {
            'profile': {'phone': '9999999999'}, 'account': {'kyc': 'verified', 'active': False},
            'role': 'ADMIN',
        }, format='json')
        self.vendor.refresh_from_db()
        self.assertEqual((self.vendor.phone, self.vendor.kyc, self.vendor.is_active, self.vendor.role),
                         ('9000000501', 'pending', True, User.Role.VENDOR))

    def test_customers_are_refused(self):
        customer = User.objects.create_user(phone='9000000502', name='C', email='c@example.com')
        self.client.force_authenticate(user=customer)
        self.assertEqual(self.client.get('/api/vendors/me/settings').status_code, 403)

    def test_booking_alerts_follow_the_preferences(self):
        from bookings.notifications import notify_booking_confirmed

        listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='arena', record={'name': 'Arena'}, name='Arena',
        )
        booking = Booking.objects.create(
            listing=listing, date=today_ist() + datetime.timedelta(days=1),
            slots=['10:00 – 11:00'], amount=620, venue_name='Arena', phone='9100000001',
        )
        VendorSettings.objects.create(
            user=self.vendor, business_email='desk@arena.example', notify_booking_sms=False,
        )
        with mock.patch('bookings.notifications._send', return_value=True) as send:
            notify_booking_confirmed(booking)
        targets = [(call.args[0], call.args[1]) for call in send.call_args_list]
        self.assertIn(('email', 'desk@arena.example'), targets)          # business email wins
        self.assertNotIn(('sms', '9000000501'), targets)                 # SMS switched off
        self.assertIn(('sms', ''), targets)
