"""
Vendors submit events; a super admin approves them; only approved events are
public and bookable — enforced by the API, not by hidden buttons.
"""
import datetime
import uuid
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from bookings.slots import today_ist
from venues.models import Listing

from .models import Event, EventOrder
from .tests import (
    EventTestCase,
    admin_client,
    auth,
    make_admin,
    make_customer,
    make_event,
    make_vendor,
)

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def payload(**over):
    body = {
        'name': 'Vendor Garba Night',
        'category': 'Dandiya',
        'description': 'Live dhol, garba till late.',
        'startTime': '18:00',
        'endTime': '22:00',
        'venueName': 'Railway Grounds',
        'locality': 'Kazipet',
        'city': 'Warangal',
        'posterUrl': 'https://cdn.example.com/garba.jpg',
        'dates': [{'date': (today_ist() + datetime.timedelta(days=3)).isoformat(), 'capacity': 150}],
        'ticketTypes': [
            {'key': 'adult', 'label': 'Adult', 'price': 400},
            {'key': 'child', 'label': 'Child', 'price': 200},
        ],
    }
    body.update(over)
    return body


class VendorSubmissionTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor('9000000401', 'Garba Vendor')
        self.other = make_vendor('9000000402', 'Other Vendor')
        self.customer = make_customer('9100000401')
        self.client_v = auth(self.vendor)

    def submit(self, **over):
        return self.client_v.post(reverse('vendor-events'), payload(**over), format='json')

    def test_a_vendor_submits_an_event_and_it_awaits_approval(self):
        r = self.submit()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['event']['status'], 'pending')
        self.assertTrue(r.data['event']['canEdit'])
        event = Event.objects.get(pk=r.data['event']['id'])
        self.assertEqual((event.vendor, event.created_by, event.status),
                         (self.vendor, self.vendor, Event.Status.PENDING))
        self.assertEqual(event.ticket_types.count(), 2)
        listing = self.client_v.get(reverse('vendor-events')).data
        self.assertTrue(listing['canCreate'])
        self.assertIn(str(event.pk), [e['id'] for e in listing['events']])

    def test_the_body_cannot_choose_owner_status_promotion_or_organiser(self):
        r = self.submit(status='live', publish=True, featured=True, vendor=self.other.id,
                        vendorId=self.other.id, organizerName='Someone Else',
                        organizerPhone='9999999999', reviewNotes='approved!')
        event = Event.objects.get(pk=r.data['event']['id'])
        self.assertEqual(event.vendor, self.vendor)
        self.assertEqual(event.status, Event.Status.PENDING)
        self.assertFalse(event.featured)
        self.assertEqual((event.organizer_name, event.organizer_phone, event.review_notes), ('', '', ''))
        self.assertEqual(event.organizer, 'Garba Vendor')        # the account's own name

    def test_unauthenticated_and_customer_cannot_submit(self):
        self.assertEqual(APIClient().post(reverse('vendor-events'), payload(), format='json').status_code, 401)
        self.assertEqual(auth(self.customer).post(reverse('vendor-events'), payload(), format='json').status_code, 403)
        self.assertEqual(Event.objects.count(), 0)

    def test_a_vendor_can_only_link_their_own_venue(self):
        foreign = Listing.objects.create(id=uuid.uuid4(), vendor=self.other, slug='other-hall',
                                         record={'name': 'Other Hall'}, name='Other Hall')
        r = self.submit(venueId=str(foreign.id))
        self.assertEqual(r.status_code, 400)
        self.assertIn('your own venues', r.data['message'])

    def test_unsafe_image_urls_are_refused(self):
        self.assertEqual(self.submit(posterUrl='javascript:alert(1)').status_code, 400)
        self.assertEqual(self.submit(gallery=['data:image/png;base64,AAAA']).status_code, 400)

    def test_validation_matches_the_admin_rules(self):
        self.assertEqual(self.submit(name='').status_code, 400)
        past = (today_ist() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self.submit(dates=[{'date': past, 'capacity': 10}]).status_code, 400)
        self.assertEqual(self.submit(ticketTypes=[{'key': 'adult', 'price': 100}]).status_code, 400)


class PendingAndRejectedAreNeverPublicTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor('9000000403', 'V')
        self.customer = make_customer('9100000403')
        r = auth(self.vendor).post(reverse('vendor-events'), payload(), format='json')
        self.event = Event.objects.get(pk=r.data['event']['id'])

    def assert_hidden_and_unbookable(self):
        public = APIClient()
        listed = [e['id'] for e in public.get('/api/events', {'limit': 50}).data['events']]
        self.assertNotIn(str(self.event.pk), listed)
        searched = [e['id'] for e in public.get('/api/events', {'q': 'Garba'}).data['events']]
        self.assertNotIn(str(self.event.pk), searched)
        self.assertEqual(public.get(f'/api/events/{self.event.slug}').status_code, 404)
        self.assertEqual(public.get(f'/api/events/{self.event.pk}').status_code, 404)
        date = self.event.dates.first()
        self.assertEqual(public.get(f'/api/events/{self.event.pk}/availability',
                                    {'date': date.date.isoformat()}).status_code, 404)
        with patch('events.payments.create_order', return_value='order_never') as gateway:
            r = auth(self.customer).post(reverse('event-payment-order'), {
                'eventId': str(self.event.pk), 'eventDateId': date.id, 'tickets': {'adult': 1},
            }, format='json')
            gateway.assert_not_called()
        self.assertGreaterEqual(r.status_code, 400)
        self.assertEqual(EventOrder.objects.filter(event=self.event).count(), 0)

    def test_pending_event_is_hidden_and_cannot_be_booked(self):
        self.assert_hidden_and_unbookable()

    def test_rejected_event_is_hidden_and_cannot_be_booked(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.REJECTED)
        self.assert_hidden_and_unbookable()


class ApprovalTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor('9000000404', 'Approval Vendor')
        self.customer = make_customer('9100000404')
        self.admin = make_admin()
        self.admin_c = admin_client(self.admin)
        r = auth(self.vendor).post(reverse('vendor-events'), payload(), format='json')
        self.event_id = r.data['event']['id']

    def patch_admin(self, body, client=None):
        return (client or self.admin_c).patch(f'/api/admin/events/{self.event_id}', body, format='json')

    def test_admin_sees_the_submission_with_its_vendor(self):
        rows = self.admin_c.get('/api/admin/events').data['events']
        row = next(e for e in rows if e['id'] == self.event_id)
        self.assertEqual((row['status'], row['managedBy'], row['vendor'], row['vendorPhone']),
                         ('pending', 'vendor', 'Approval Vendor', '9000000404'))

    def test_admin_approves_then_it_is_public_and_bookable(self):
        r = self.patch_admin({'status': 'approved'})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['event']['status'], 'live')
        listed = [e['id'] for e in APIClient().get('/api/events').data['events']]
        self.assertIn(self.event_id, listed)
        event = Event.objects.get(pk=self.event_id)
        with patch('events.payments.create_order', return_value='order_ok1'):
            r = auth(self.customer).post(reverse('event-payment-order'), {
                'eventId': self.event_id, 'eventDateId': event.dates.first().id, 'tickets': {'adult': 2},
            }, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        order = EventOrder.objects.get(pk=r.data['eventOrderId'])
        self.assertEqual(order.status, 'payment_pending')         # paid only after verification

    def test_rejection_needs_a_reason_the_vendor_then_sees(self):
        self.assertEqual(self.patch_admin({'status': 'rejected'}).status_code, 400)
        r = self.patch_admin({'status': 'rejected', 'reviewNotes': 'Poster is blurry — please re-upload.'})
        self.assertEqual(r.status_code, 200)
        mine = auth(self.vendor).get(reverse('vendor-event-detail', args=[self.event_id])).data['event']
        self.assertEqual((mine['status'], mine['reviewNotes']), ('rejected', 'Poster is blurry — please re-upload.'))
        # Fixing and resubmitting puts it back in the queue and clears the note.
        r = auth(self.vendor).patch(reverse('vendor-event-detail', args=[self.event_id]),
                                    {'posterUrl': 'https://cdn.example.com/sharp.jpg'}, format='json')
        self.assertEqual((r.data['event']['status'], r.data['event']['reviewNotes']), ('pending', ''))

    def test_only_an_admin_can_approve(self):
        vendor_jwt = auth(self.vendor)
        self.assertEqual(self.patch_admin({'status': 'approved'}, client=vendor_jwt).status_code, 403)
        self.assertEqual(self.patch_admin({'status': 'approved'}, client=APIClient()).status_code, 403)
        session = APIClient()
        session.force_login(self.vendor)
        self.assertEqual(self.patch_admin({'status': 'approved'}, client=session).status_code, 403)
        # …and the vendor endpoint ignores any status the vendor sends.
        r = vendor_jwt.patch(reverse('vendor-event-detail', args=[self.event_id]), {'status': 'live'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Event.objects.get(pk=self.event_id).status, Event.Status.PENDING)

    def test_editing_an_approved_event_sends_it_back_for_review(self):
        self.patch_admin({'status': 'approved', 'featured': True})
        r = auth(self.vendor).patch(reverse('vendor-event-detail', args=[self.event_id]),
                                    {'name': 'Renamed Garba', 'ticketTypes': [
                                        {'key': 'adult', 'label': 'Adult', 'price': 1},
                                        {'key': 'child', 'label': 'Child', 'price': 1}]}, format='json')
        self.assertTrue(r.data['resubmitted'])
        event = Event.objects.get(pk=self.event_id)
        self.assertEqual((event.status, event.featured), (Event.Status.PENDING, False))
        listed = [e['id'] for e in APIClient().get('/api/events').data['events']]
        self.assertNotIn(self.event_id, listed)

    def test_admin_still_creates_events_directly(self):
        r = self.admin_c.post('/api/admin/events', {**payload(name='Admin Night'), 'status': 'publish'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual((r.data['event']['status'], r.data['event']['managedBy']), ('live', 'admin'))


class OwnershipAndTicketHolderTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor('9000000405', 'Owner V')
        self.other = make_vendor('9000000406', 'Other V')
        self.event = make_event(self.vendor, name='Owned Night', capacity=50)

    def detail(self, client, event=None):
        return reverse('vendor-event-detail', args=[(event or self.event).pk])

    def test_another_vendor_cannot_edit_or_delete(self):
        other = auth(self.other)
        self.assertEqual(other.patch(self.detail(other), {'name': 'Hijack'}, format='json').status_code, 404)
        self.assertEqual(other.delete(self.detail(other)).status_code, 404)

    def test_admin_event_at_my_venue_is_read_only_for_me(self):
        venue = Listing.objects.create(id=uuid.uuid4(), vendor=self.vendor, slug='my-hall',
                                       record={'name': 'My Hall'}, name='My Hall')
        hosted = make_event(None, name='Admin Hosted', listing=venue)
        client = auth(self.vendor)
        self.assertFalse(next(e for e in client.get(reverse('vendor-events')).data['events']
                              if e['id'] == str(hosted.pk))['canEdit'])
        r = client.patch(self.detail(client, hosted), {'name': 'Mine now'}, format='json')
        self.assertEqual((r.status_code, r.data['code']), (403, 'NOT_OWNER'))

    def sell(self, qty=5):
        date = self.event.dates.first()
        return EventOrder.objects.create(event=self.event, event_date=date, date=date.date,
                                         status='confirmed', seats=qty, ticket_count=qty,
                                         amount=100, razorpay_payment_id='pay_x')

    def test_sold_dates_cannot_be_shrunk_below_sales(self):
        self.sell(5)
        date = self.event.dates.first()
        r = auth(self.vendor).patch(self.detail(None), {'dates': [
            {'date': date.date.isoformat(), 'capacity': 3}]}, format='json')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.event.dates.first().capacity, 50)

    def test_withdraw_only_without_sales(self):
        client = auth(self.vendor)
        self.sell(1)
        r = client.delete(self.detail(client))
        self.assertEqual((r.status_code, r.data['code']), (409, 'HAS_SALES'))
        unsold = make_event(self.vendor, name='Unsold Night')
        self.assertEqual(client.delete(self.detail(client, unsold)).status_code, 200)
        self.assertEqual(Event.objects.get(pk=unsold.pk).status, Event.Status.DELETED)

    def test_image_upload_checks_the_file_and_returns_a_url(self):
        client = auth(self.vendor)
        with patch('events.vendor_views.upload_photo', return_value='https://cdn.example.com/x.png') as store:
            r = client.post(reverse('vendor-event-images'),
                            {'file': SimpleUploadedFile('p.png', PNG, content_type='image/png')},
                            format='multipart')
            self.assertEqual((r.status_code, r.data['url']), (201, 'https://cdn.example.com/x.png'))
            fake = SimpleUploadedFile('p.png', b'not an image at all', content_type='image/png')
            self.assertEqual(client.post(reverse('vendor-event-images'), {'file': fake},
                                         format='multipart').status_code, 400)
            self.assertEqual(store.call_count, 1)
        other = auth(self.other)
        r = other.post(reverse('vendor-event-poster', args=[self.event.pk]),
                       {'file': SimpleUploadedFile('p.png', PNG, content_type='image/png')}, format='multipart')
        self.assertEqual(r.status_code, 404)
