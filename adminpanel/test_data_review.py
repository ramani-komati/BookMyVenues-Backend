"""
QA audit 2026-10-30 — test data in production. The review only LISTS
candidates (it never changes or deletes anything); genuine records are not
flagged; it is admin-only.
"""
import datetime
import uuid

from rest_framework.test import APIClient, APITestCase

from accounts.models import User
from events.models import Event, EventDate, EventOrder
from venues.models import Listing

URL = '/api/admin/data-review'


class DataReviewTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(phone='9777700001', name='Admin', role=User.Role.ADMIN, password='pw')
        self.vendor = User.objects.create_user(phone='9777700002', name='V', role=User.Role.VENDOR)
        self.client.force_login(self.admin)

    def listing(self, name, price):
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug=f's-{uuid.uuid4().hex[:6]}',
            record={'name': name, 'price': price, 'unit': '/ hour'}, name=name,
            category='Play zone', status='live')

    def test_flags_the_audit_records_and_nothing_genuine(self):
        demo = self.listing('Durga Play Ground Demo Venue', '1')
        self.listing('Stayfit Sports Zone', '500')
        brand = Event.objects.create(name='bookmyvenue', slug='bmv-x', venue_name='ku',
                                     start_time=datetime.time(18), end_time=datetime.time(20), status='live')
        real = Event.objects.create(name='Comedy Night', slug='comedy-x', venue_name='RK Party House',
                                    start_time=datetime.time(18), end_time=datetime.time(20), status='live')
        date = EventDate.objects.create(event=brand, date=datetime.date(2026, 10, 1))
        buyer = User.objects.create_user(phone='9777700003', name='Buyer')
        order = EventOrder.objects.create(event=brand, event_date=date, user=buyer, event_name='bookmyvenue',
                                          date=date.date, status='payment_pending', amount=1)
        EventOrder.objects.create(event=real, user=buyer, event_name='Comedy Night',
                                  date=date.date, status='confirmed', amount=300)

        data = self.client.get(URL).data
        venues = {v['name']: v for v in data['venues']}
        self.assertEqual(set(venues), {'Durga Play Ground Demo Venue'})
        self.assertIn('Name contains "Demo"', venues['Durga Play Ground Demo Venue']['reasons'])
        self.assertIn('Price is ₹1', venues['Durga Play Ground Demo Venue']['reasons'])
        events = {e['name']: e for e in data['events']}
        self.assertEqual(set(events), {'bookmyvenue'})
        self.assertIn('Named after the platform itself', events['bookmyvenue']['reasons'])
        self.assertEqual([o['id'] for o in data['orders']], [order.pk])

        # Read-only: nothing changed.
        demo.refresh_from_db()
        brand.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual((demo.status, brand.status, order.status), ('live', 'live', 'payment_pending'))

    def test_admin_only(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))
        vendor = APIClient()
        vendor.force_login(self.vendor)
        self.assertIn(vendor.get(URL).status_code, (401, 403))
