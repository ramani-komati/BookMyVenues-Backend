"""
Vendor / organiser contact in the customer's booking history — revealed only
after the payment is verified, only to the booking's own customer, and never
on public pages.
"""
import datetime
import hashlib
import hmac
import json
import uuid
from unittest.mock import MagicMock, patch

from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User
from events.models import Event, EventDate, EventOrder, TicketType
from venues.models import Listing

from .models import Booking
from .slots import today_ist
from .test_checkin import CheckInTestCase, jwt_client, make_booking

TOMORROW = (today_ist() + datetime.timedelta(days=1)).isoformat()


def history(client):
    return {b['id']: b for b in client.get('/api/users/me/bookings').data['bookings']}


def tickets(client):
    return {o['id']: o for o in client.get('/api/users/me/event-orders').data['orders']}


class VenueContactTests(CheckInTestCase):
    """ABC Owner (9000000201) runs ABC Venue; XYZ Owner (9000000202) runs XYZ."""

    def test_paid_booking_shows_the_right_owner(self):
        row = history(jwt_client(self.customer_a))[self.booking_a.id]
        self.assertEqual(row['vendorContact'],
                         {'role': 'Venue owner', 'name': 'ABC Owner', 'phone': '9000000201'})
        row_b = history(jwt_client(self.customer_b))[self.booking_b.id]
        self.assertEqual(row_b['vendorContact']['phone'], '9000000202')   # never another vendor's

    def test_every_unpaid_or_reversed_state_shows_nothing(self):
        client = jwt_client(self.customer_a)
        for state in ('failed', 'cancelled', 'refund_pending', 'refunded'):
            booking = make_booking(self.abc, self.customer_a, status=state)
            self.assertIsNone(history(client)[booking.id]['vendorContact'], state)
        # A 'confirmed' row with no captured payment (legacy pay-at-venue, or
        # anything not verified by the gateway) does not unlock it either.
        for extra in ({'razorpay_payment_id': ''}, {'method': 'venue'}):
            booking = make_booking(self.abc, self.customer_a, **extra)
            self.assertIsNone(history(client)[booking.id]['vendorContact'], extra)

    def test_pending_payment_rows_are_not_even_listed(self):
        pending = make_booking(self.abc, self.customer_a, status='payment_pending')
        self.assertNotIn(pending.id, history(jwt_client(self.customer_a)))

    def test_a_customer_never_sees_another_customers_booking(self):
        self.assertNotIn(self.booking_b.id, history(jwt_client(self.customer_a)))
        self.assertEqual(APIClient().get('/api/users/me/bookings').status_code, 401)

    def test_every_category_uses_its_own_owner(self):
        for category in ('Party hall', 'Private theatre', 'Play zone', 'Resort', 'Playstation'):
            owner = User.objects.create_user(phone=f'90000005{len(category):02d}', name=f'{category} Owner',
                                             role=User.Role.VENDOR, is_vendor=True)
            venue = Listing.objects.create(id=uuid.uuid4(), vendor=owner, slug=f'v-{uuid.uuid4().hex[:6]}',
                                           record={'name': category}, name=category, category=category)
            booking = make_booking(venue, self.customer_a)
            contact = history(jwt_client(self.customer_a))[booking.id]['vendorContact']
            self.assertEqual((contact['name'], contact['phone']), (f'{category} Owner', owner.phone), category)

    def test_public_venue_pages_never_carry_a_phone(self):
        record = {**self.abc.record, 'contactPhone': '9000000201',
                  'detail': {**self.abc.record['detail'], 'contactPhone': '9000000201'}}
        Listing.objects.filter(pk=self.abc.pk).update(record=record, status='live')
        public = APIClient()
        listing = json.dumps(public.get('/api/venues', {'limit': 50}).data)
        detail = json.dumps(public.get(f'/api/venues/{self.abc.slug}').data)
        for text in (listing, detail):
            self.assertNotIn('9000000201', text)
            self.assertNotIn('9000000202', text)


RZP = override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='secret_c')


@RZP
class PaymentUnlocksContactTests(CheckInTestCase):
    """Only the server's own verified payment unlocks the contact — not a
    browser claiming success."""

    def order(self, customer):
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': 'order_CONTACT1'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            r = customer.post('/api/payments/order', {
                'venueId': str(self.abc.id), 'date': TOMORROW, 'slots': ['19:30 – 21:00'],
                'addons': [], 'amount': 920, 'perSlot': 600,
            }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['bookingId']

    def test_forged_success_does_not_unlock_but_a_verified_payment_does(self):
        customer = jwt_client(self.customer_b)
        booking_id = self.order(customer)
        forged = customer.post('/api/payments/verify', {
            'bookingId': booking_id, 'razorpay_order_id': 'order_CONTACT1',
            'razorpay_payment_id': 'pay_fake', 'razorpay_signature': 'not-a-real-signature',
        }, format='json')
        self.assertEqual(forged.status_code, 400)
        self.assertNotIn(booking_id, history(customer))                  # still unpaid → hidden
        Booking.objects.filter(pk=booking_id).update(status='failed')    # a failed attempt
        self.assertIsNone(history(customer)[booking_id]['vendorContact'])

        signature = hmac.new(b'secret_c', b'order_CONTACT1|pay_real', hashlib.sha256).hexdigest()
        ok = customer.post('/api/payments/verify', {
            'bookingId': booking_id, 'razorpay_order_id': 'order_CONTACT1',
            'razorpay_payment_id': 'pay_real', 'razorpay_signature': signature,
        }, format='json')
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(history(customer)[booking_id]['vendorContact']['phone'], '9000000201')

        # A refund revokes it again.
        Booking.objects.filter(pk=booking_id).update(status='refunded')
        self.assertIsNone(history(customer)[booking_id]['vendorContact'])


class EventContactTests(CheckInTestCase):
    def make_event(self, **fields):
        event = Event.objects.create(slug=f'ev-{uuid.uuid4().hex[:6]}', name=fields.pop('name', 'Night'),
                                     category='Dandiya', venue_name='Ground',
                                     start_time=datetime.time(18, 0), end_time=datetime.time(22, 0),
                                     status=Event.Status.LIVE, **fields)
        EventDate.objects.create(event=event, date=today_ist(), capacity=50)
        TicketType.objects.create(event=event, key='adult', label='Adult', price=300)
        return event

    def order(self, event, customer, **extra):
        fields = dict(event=event, user=customer, event_name=event.name, date=today_ist(),
                      status='confirmed', razorpay_payment_id='pay_e', seats=1, ticket_count=1, amount=320)
        fields.update(extra)
        return EventOrder.objects.create(**fields)

    def test_vendor_submitted_event_shows_the_vendor(self):
        event = self.make_event(vendor=self.xyz_vendor)
        order = self.order(event, self.customer_a)
        contact = tickets(jwt_client(self.customer_a))[order.id]['vendorContact']
        self.assertEqual(contact, {'role': 'Organiser', 'name': 'XYZ Owner', 'phone': '9000000202'})

    def test_admin_event_shows_the_recorded_organiser(self):
        event = self.make_event(name='Admin Garba', organizer_name='Garba Club', organizer_phone='9876500000')
        order = self.order(event, self.customer_a)
        contact = tickets(jwt_client(self.customer_a))[order.id]['vendorContact']
        self.assertEqual((contact['name'], contact['phone']), ('Garba Club', '9876500000'))

    def test_admin_event_without_any_contact_shows_nothing_rather_than_guessing(self):
        event = self.make_event(listing=self.abc)          # hosted at ABC — NOT assumed organiser
        order = self.order(event, self.customer_a)
        self.assertIsNone(tickets(jwt_client(self.customer_a))[order.id]['vendorContact'])

    def test_unpaid_ticket_states_show_nothing(self):
        event = self.make_event(vendor=self.xyz_vendor)
        client = jwt_client(self.customer_a)
        for state in ('failed', 'cancelled', 'refund_pending', 'refunded'):
            order = self.order(event, self.customer_a, status=state)
            self.assertIsNone(tickets(client)[order.id]['vendorContact'], state)

    def test_public_event_pages_never_carry_a_phone(self):
        event = self.make_event(vendor=self.xyz_vendor, contact_phone='9000000202',
                                organizer_phone='9000000202')
        public = APIClient()
        listing = json.dumps(public.get('/api/events').data)
        detail = json.dumps(public.get(f'/api/events/{event.slug}').data)
        self.assertNotIn('9000000202', listing)
        self.assertNotIn('9000000202', detail)
