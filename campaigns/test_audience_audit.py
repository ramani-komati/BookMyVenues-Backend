"""
QA audit 2026-10-30 — the audience selector showed 0 eligible customers and
gave the admin no way to see who qualifies.

  * PUBLIC-role customers (the admin Users list counts them) are eligible;
  * count == recipient list == the list a send freezes (one per address,
    valid addresses only, opted-in only, suppressed excluded);
  * interest segments use bookings, favourites and event tickets only, and a
    customer with none shows "No recorded interests";
  * the recipient list is admin-only, searchable and paginated;
  * nothing is sent by choosing or previewing an audience.
"""
import datetime
import uuid
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from events.models import Event, EventDate, EventOrder
from venues.models import Favorite

from .audience import NO_INTERESTS, clean_audience, estimate, recipient_page, recipients
from .models import Campaign, CampaignRecipient, EmailSuppression
from .tests import DRY, CampaignTestBase

URL = '/api/admin/campaigns/audience/recipients'


class AudienceCountTests(CampaignTestBase):

    def test_public_role_customers_are_eligible(self):
        user = User.objects.create_user(phone='9188800001', name='Pub', email='pub@mail.in',
                                        role=User.Role.PUBLIC)
        from .models import EmailPreference
        EmailPreference.objects.create(user=user, marketing_opt_in=True, consented_at=timezone.now())
        self.assertFalse(user.is_customer)
        self.assertEqual(estimate({'segment': 'all'})['count'], 1)

    def test_opted_out_suppressed_invalid_and_duplicate_addresses_are_excluded(self):
        self.customer(name='In', email='in@mail.in')
        self.customer(name='Out', email='out@mail.in', opted_in=False)
        self.customer(name='Never', email='never@mail.in', opted_in=None)
        self.customer(name='Bounced', email='bounce@mail.in')
        EmailSuppression.objects.create(email='bounce@mail.in', reason='bounce')
        self.customer(name='Broken', email='not-an-email')
        self.customer(name='Dup1', email='Same@Mail.in')
        self.customer(name='Dup2', email='same@mail.in')
        result = estimate({'segment': 'all'})
        emails = [email for _uid, email, _name in recipients({'segment': 'all'})]
        self.assertEqual(sorted(emails), ['in@mail.in', 'same@mail.in'])
        self.assertEqual(result['count'], 2)          # matches the list exactly
        self.assertEqual(result['invalidEmail'], 1)
        self.assertEqual(result['duplicates'], 1)

    def test_count_list_and_send_agree(self):
        self.customer(name='A', email='dup@mail.in')
        self.customer(name='B', email='DUP@mail.in')
        self.customer(name='C', email='c@mail.in')
        count = estimate({'segment': 'all'})['count']
        page = self.admin_client.post(URL, {'audience': {'segment': 'all'}}, format='json').data
        self.assertEqual(page['total'], count)
        campaign = self.campaign(audience={'segment': 'all'})
        with DRY:
            response = self.admin_client.post(
                f'/api/admin/campaigns/{campaign.pk}/send',
                {'confirm': True, 'confirmAll': True, 'expectedCount': count,
                 'version': campaign.version}, format='json')
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(CampaignRecipient.objects.filter(campaign=campaign).count(), count)


class InterestSegmentTests(CampaignTestBase):

    def setUp(self):
        super().setUp()
        self.hall = self.listing('Grand Hall', city='Hanamkonda', category='Party hall')
        self.turf = self.listing('Turf', city='Warangal', category='Play zone')

    def test_category_interest_from_bookings_and_favourites(self):
        booker = self.customer(name='Booker')
        self.book(booker, self.hall)
        saver = self.customer(name='Saver')
        Favorite.objects.create(user=saver, listing=self.turf, added_at=timezone.now())
        self.customer(name='Nobody')
        names = lambda aud: sorted(n for _u, _e, n in recipients(aud))  # noqa: E731
        self.assertEqual(names({'segment': 'interest_category', 'categories': ['Party hall']}), ['Booker'])
        self.assertEqual(names({'segment': 'interest_category', 'categories': ['Play zone']}), ['Saver'])
        self.assertEqual(
            names({'segment': 'interest_category', 'categories': ['Party hall', 'Play zone']}),
            ['Booker', 'Saver'])

    def test_city_interest_and_unpaid_bookings_do_not_count(self):
        paid = self.customer(name='Paid')
        self.book(paid, self.turf)
        unpaid = self.customer(name='Unpaid')
        self.book(unpaid, self.turf, status='payment_pending')
        names = [n for _u, _e, n in recipients({'segment': 'interest_city', 'cities': ['warangal']})]
        self.assertEqual(names, ['Paid'])

    def test_event_buyers_are_active_and_city_interested(self):
        buyer = self.customer(name='Ticket Buyer')
        event = Event.objects.create(
            name='Comedy Night', slug=f'comedy-{uuid.uuid4().hex[:4]}', city='Hanamkonda',
            start_time=datetime.time(19, 0), end_time=datetime.time(21, 0), status='live')
        date = EventDate.objects.create(event=event, date=timezone.now().date())
        EventOrder.objects.create(event=event, event_date=date, user=buyer, event_name='Comedy Night',
                                  date=timezone.now().date(), status='confirmed', amount=300)
        self.assertEqual([n for _u, _e, n in recipients({'segment': 'active'})], ['Ticket Buyer'])
        self.assertEqual(
            [n for _u, _e, n in recipients({'segment': 'interest_city', 'cities': ['Hanamkonda']})],
            ['Ticket Buyer'])
        row = recipient_page({'segment': 'event_buyers'})['recipients'][0]
        self.assertIn('Comedy Night', row['reason'])

    def test_rows_explain_location_interests_and_reason(self):
        booker = self.customer(name='Booker', email='booker@mail.in')
        self.book(booker, self.hall)
        self.customer(name='Quiet', email='quiet@mail.in')
        rows = {r['name']: r for r in recipient_page({'segment': 'all'})['recipients']}
        self.assertEqual(rows['Booker']['city'], 'Hanamkonda')
        self.assertEqual(rows['Booker']['state'], 'Telangana')
        self.assertEqual(rows['Booker']['interests'], ['Party hall'])
        self.assertEqual(rows['Quiet']['interests'], [NO_INTERESTS])   # never invented
        self.assertFalse(rows['Quiet']['hasInterests'])
        self.assertEqual(rows['Booker']['reason'], 'Opted in to promotional emails')
        booked = recipient_page({'segment': 'booked'})['recipients']
        self.assertEqual([r['reason'] for r in booked], ['1 confirmed booking'])

    def test_validation(self):
        from .audience import AudienceError
        with self.assertRaises(AudienceError):
            clean_audience({'segment': 'interest_city', 'cities': []})
        with self.assertRaises(AudienceError):
            clean_audience({'segment': 'interest_category', 'categories': ['x'] * 0})
        self.assertEqual(
            clean_audience({'segment': 'interest_category', 'categories': ['Resort', 'resort', ' ']}),
            {'segment': 'interest_category', 'categories': ['Resort']})


class RecipientListEndpointTests(CampaignTestBase):

    def test_admin_sees_full_details_paginated_and_searchable(self):
        for i in range(30):
            self.customer(name=f'Cust {i:02d}', email=f'cust{i:02d}@mail.in')
        first = self.admin_client.post(URL, {'audience': {'segment': 'all'}}, format='json')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data['total'], 30)
        self.assertEqual(len(first.data['recipients']), 25)
        second = self.admin_client.post(URL, {'audience': {'segment': 'all'}, 'page': 2}, format='json')
        self.assertEqual(len(second.data['recipients']), 5)
        found = self.admin_client.post(URL, {'audience': {'segment': 'all'}, 'search': 'cust07'},
                                       format='json').data
        self.assertEqual(found['matched'], 1)
        self.assertEqual(found['recipients'][0]['email'], 'cust07@mail.in')

    def test_customers_vendors_and_anonymous_are_refused(self):
        self.customer(name='Secret', email='secret@mail.in')
        anonymous = APIClient()
        self.assertIn(anonymous.post(URL, {'audience': {'segment': 'all'}}, format='json').status_code,
                      (401, 403))
        vendor_client = APIClient()
        vendor_client.force_login(self.vendor)
        response = vendor_client.post(URL, {'audience': {'segment': 'all'}}, format='json')
        self.assertIn(response.status_code, (401, 403))
        self.assertNotIn('secret@mail.in', str(response.content))

    def test_empty_audience_is_a_clean_empty_page(self):
        response = self.admin_client.post(URL, {'audience': {'segment': 'booked'}}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 0)
        self.assertEqual(response.data['recipients'], [])

    def test_choosing_and_listing_an_audience_never_sends(self):
        self.customer(name='A')
        with mock.patch('campaigns.resend_client.requests.post') as post, DRY:
            self.admin_client.post('/api/admin/campaigns/audience',
                                   {'audience': {'segment': 'all'}}, format='json')
            self.admin_client.post(URL, {'audience': {'segment': 'all'}}, format='json')
            self.admin_client.post('/api/admin/campaigns',
                                   {'name': 'Draft', 'audience': {'segment': 'all'}}, format='json')
        post.assert_not_called()
        self.assertFalse(CampaignRecipient.objects.exists())
        self.assertFalse(Campaign.objects.exclude(status=Campaign.Status.DRAFT).exists())
