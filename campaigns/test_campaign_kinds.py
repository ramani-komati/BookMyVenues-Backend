"""
Promotional vs service campaigns (2026-11-03).

  * PROMOTIONAL — only customers who opted in themselves; the panel shows
    how many are eligible and, for everyone else, why not.
  * SERVICE — booking / payment / account notices reach every registered
    customer with a working address, WITHOUT marketing consent, but never a
    bounced / complained address or a deactivated account, only through
    service segments, and never with offer wording.
  * Neither kind ever changes a customer's marketing consent, and an offers
    unsubscribe keeps working exactly as before.

No real email: CAMPAIGN_DELIVERY is 'off' in test settings, and the tests
that send patch `requests.post` inside campaigns.resend_client.
"""
from unittest import mock

from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import User

from . import content, sender
from .audience import (
    PROMOTIONAL,
    SERVICE,
    AudienceError,
    clean_audience,
    eligibility,
    estimate,
    recipients,
)
from .models import Campaign, CampaignRecipient, EmailPreference, EmailSuppression
from .public_views import opt_out
from .tests import SEND, CampaignTestBase, batch_ok

SERVICE_SEND = override_settings(CAMPAIGN_DELIVERY='resend', RESEND_API_KEY='re_fake_key',
                                 RESEND_FROM='bookings@example.com',
                                 CAMPAIGN_FROM='offers@example.com', CAMPAIGN_SERVICE_FROM='')


class KindFixtures(CampaignTestBase):

    def population(self):
        """One customer per eligibility case."""
        self.opted_in = self.customer('Opted In', email='in@mail.in')
        self.never = self.customer('Never Asked', email='never@mail.in', opted_in=None)
        self.declined = self.customer('Declined', email='declined@mail.in', opted_in=False)
        self.unsubscribed = self.customer('Unsub', email='unsub@mail.in')
        opt_out(self.unsubscribed.pk, EmailPreference.Source.UNSUBSCRIBE_LINK)
        self.bounced = self.customer('Bounced', email='bounced@mail.in', opted_in=None)
        EmailSuppression.objects.create(email='bounced@mail.in', reason='bounce')
        self.no_email = self.customer('No Email', email='', opted_in=None)
        self.broken = self.customer('Broken', email='not-an-email', opted_in=None)
        self.inactive = self.customer('Blocked', email='blocked@mail.in', is_active=False)
        self.dup = self.customer('Dup', email='IN@mail.in')          # same address as opted_in
        # Not customers: never reachable by any campaign.
        User.objects.create_user(phone='9990000298', email='vendor@mail.in', role=User.Role.VENDOR)

    def service_campaign(self, **fields):
        defaults = dict(
            name='Maintenance notice', subject='Scheduled maintenance on Sunday',
            heading='Hi {{first_name}}, a quick service update',
            body='Bookings are paused from 2 to 3 am on Sunday while we upgrade our systems.',
            images=[], cta_label='', cta_url='', kind=Campaign.Kind.SERVICE,
            service_topic=Campaign.ServiceTopic.ANNOUNCEMENT, audience={'segment': 'all'})
        defaults.update(fields)
        return self.campaign(**defaults)

    def consent_snapshot(self):
        return sorted(EmailPreference.objects.values_list(
            'user_id', 'marketing_opt_in', 'consented_at', 'unsubscribed_at', 'source'))

    def send_kind(self, campaign, **body):
        count = len(recipients(campaign.audience, campaign.kind))
        payload = {'confirm': True, 'expectedCount': count, 'version': campaign.version,
                   'confirmAll': True, **body}
        return self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/send', payload,
                                      format='json')


# ---------------------------------------------------------------------------
# Recipient eligibility
# ---------------------------------------------------------------------------

class EligibilityTests(KindFixtures):

    def setUp(self):
        super().setUp()
        self.population()

    def emails(self, kind, audience=None):
        return sorted(e for _uid, e, _n in recipients(audience or {'segment': 'all'}, kind))

    def test_promotional_reaches_only_opted_in_customers(self):
        self.assertEqual(self.emails(PROMOTIONAL), ['in@mail.in'])

    def test_service_reaches_registered_customers_without_marketing_consent(self):
        self.assertEqual(self.emails(SERVICE),
                         ['declined@mail.in', 'in@mail.in', 'never@mail.in', 'unsub@mail.in'])

    def test_service_still_excludes_suppressed_inactive_invalid_and_non_customers(self):
        emails = self.emails(SERVICE)
        for excluded in ('bounced@mail.in', 'blocked@mail.in', 'not-an-email', 'vendor@mail.in',
                         'anita@admin.in'):
            self.assertNotIn(excluded, emails)

    def test_promotional_breakdown_explains_every_ineligible_customer(self):
        result = eligibility(PROMOTIONAL)
        counts = {row['key']: row['count'] for row in result['ineligible']}
        self.assertEqual(result['eligible'], 1)
        self.assertEqual(counts, {
            'inactive': 1, 'no_email': 1, 'invalid_email': 1, 'suppressed': 1,
            'unsubscribed': 1, 'not_opted_in': 2, 'duplicate': 1,
        })
        # Every customer account is either eligible or has exactly one reason.
        customers = User.objects.filter(is_customer=True).count()
        self.assertEqual(result['eligible'] + result['ineligibleTotal'], customers)
        self.assertTrue(all(row['explanation'] for row in result['ineligible']))

    def test_service_breakdown_has_no_consent_reasons(self):
        result = eligibility(SERVICE)
        keys = [row['key'] for row in result['ineligible']]
        self.assertNotIn('not_opted_in', keys)
        self.assertNotIn('unsubscribed', keys)
        self.assertEqual(result['eligible'], 4)
        self.assertEqual(result['eligible'], len(recipients({'segment': 'all'}, SERVICE)))

    def test_estimate_count_matches_the_list_for_both_kinds(self):
        for kind in (PROMOTIONAL, SERVICE):
            result = estimate({'segment': 'all'}, kind)
            self.assertEqual(result['kind'], kind)
            self.assertEqual(result['count'], len(recipients({'segment': 'all'}, kind)))
            self.assertEqual(result['eligibleAccounts'], result['count'])

    def test_estimate_endpoint_takes_the_kind_and_never_returns_addresses(self):
        promo = self.admin_client.post('/api/admin/campaigns/audience',
                                       {'audience': {'segment': 'all'}}, format='json')
        service = self.admin_client.post('/api/admin/campaigns/audience',
                                         {'audience': {'segment': 'all'}, 'kind': 'service'},
                                         format='json')
        self.assertEqual(promo.data['count'], 1)
        self.assertEqual(service.data['count'], 4)
        self.assertEqual(service.data['label'], 'All registered customers')
        self.assertNotIn('in@mail.in', str(service.data))
        bad = self.admin_client.post('/api/admin/campaigns/audience',
                                     {'audience': {'segment': 'all'}, 'kind': 'spam'}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_recipient_list_endpoint_follows_the_kind(self):
        page = self.admin_client.post('/api/admin/campaigns/audience/recipients',
                                      {'audience': {'segment': 'all'}, 'kind': 'service'},
                                      format='json').data
        self.assertEqual(page['total'], 4)
        self.assertEqual(page['recipients'][0]['reason'],
                         'Registered customer with a working email address')

    def test_service_cannot_use_marketing_segments(self):
        for segment in ('not_booked', 'lapsed', 'interest_category', 'interest_city'):
            raw = {'segment': segment, 'categories': ['Resort'], 'cities': ['Pune']}
            with self.assertRaises(AudienceError):
                clean_audience(raw, SERVICE)
            clean_audience(raw, PROMOTIONAL)            # still fine for promotions
        response = self.admin_client.post('/api/admin/campaigns/audience',
                                          {'audience': {'segment': 'lapsed'}, 'kind': 'service'},
                                          format='json')
        self.assertEqual(response.status_code, 400)

    def test_service_booking_segment_reaches_bookers_regardless_of_consent(self):
        hall = self.listing()
        self.book(self.never, hall)
        self.book(self.opted_in, hall)
        self.assertEqual(self.emails(SERVICE, {'segment': 'booked'}), ['in@mail.in', 'never@mail.in'])
        self.assertEqual(self.emails(PROMOTIONAL, {'segment': 'booked'}), ['in@mail.in'])

    def test_an_unsubscribe_covers_every_account_sharing_the_address(self):
        # self.dup (IN@mail.in, opted in) shares self.opted_in's address.
        opt_out(self.opted_in.pk, EmailPreference.Source.UNSUBSCRIBE_LINK)
        self.assertEqual(self.emails(PROMOTIONAL), [])
        counts = {row['key']: row['count'] for row in eligibility(PROMOTIONAL)['ineligible']}
        self.assertEqual(counts['unsubscribed'], 3)        # unsub@, in@ and its twin
        self.assertTrue(EmailPreference.objects.get(user=self.dup).marketing_opt_in)  # untouched
        self.assertIn('in@mail.in', self.emails(SERVICE))

    def test_counting_and_listing_never_change_consent(self):
        before = self.consent_snapshot()
        for kind in (PROMOTIONAL, SERVICE):
            estimate({'segment': 'all'}, kind)
            eligibility(kind)
            recipients({'segment': 'all'}, kind)
        self.assertEqual(self.consent_snapshot(), before)


# ---------------------------------------------------------------------------
# Drafts: kind, topic, content rules
# ---------------------------------------------------------------------------

class KindDraftTests(KindFixtures):

    def test_existing_and_new_campaigns_default_to_promotional(self):
        campaign = self.campaign()
        self.assertEqual(campaign.kind, 'promotional')
        row = self.admin_client.get(f'/api/admin/campaigns/{campaign.pk}').data['campaign']
        self.assertEqual(row['kind'], 'promotional')
        self.assertEqual(row['serviceTopic'], '')

    def test_switching_to_service_rechecks_the_audience_and_topic(self):
        campaign = self.campaign(audience={'segment': 'lapsed', 'days': 90})
        url = f'/api/admin/campaigns/{campaign.pk}'
        refused = self.admin_client.patch(url, {'version': 1, 'kind': 'service'}, format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data['field'], 'audience')
        ok = self.admin_client.patch(url, {'version': 1, 'kind': 'service', 'serviceTopic': 'payment',
                                           'audience': {'segment': 'booked'}}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertEqual(ok.data['campaign']['kind'], 'service')
        self.assertEqual(ok.data['campaign']['serviceTopicLabel'], 'Payment notification')
        back = self.admin_client.patch(url, {'version': 2, 'kind': 'promotional'}, format='json')
        self.assertEqual(back.data['campaign']['serviceTopic'], '')   # topic only for service
        bad = self.admin_client.patch(url, {'version': 3, 'kind': 'newsletter'}, format='json')
        self.assertEqual(bad.status_code, 400)

    def test_service_email_cannot_carry_offer_wording(self):
        clean = self.service_campaign()
        self.assertEqual(content.send_problems(clean), {})
        for text in ('Get 20% off this weekend', 'Use coupon DIWALI', 'Our biggest sale',
                     'Exclusive offers inside', 'Save ₹500 on halls', 'Use promo code X'):
            campaign = self.service_campaign(subject=text)
            self.assertIn('kind', content.send_problems(campaign), text)
        promo = self.campaign(subject='Get 20% off this weekend')
        self.assertNotIn('kind', content.send_problems(promo))

    def test_service_email_needs_a_topic(self):
        campaign = self.service_campaign(service_topic='')
        self.assertIn('serviceTopic', content.send_problems(campaign))

    def test_service_footer_has_preferences_link_and_no_unsubscribe(self):
        campaign = self.service_campaign()
        subject, html, text = content.personalize(
            content.render_template(campaign), name='Asha', unsubscribe_url=content.preferences_url())
        self.assertIn('Service announcement', html)
        self.assertIn('It is not a promotion', text)
        self.assertIn('Email preferences', html)
        self.assertIn('/account', html)
        self.assertNotIn('chose to get offers', html)
        self.assertNotIn('Unsubscribe', html)

    def test_preview_renders_the_service_footer(self):
        response = self.admin_client.post('/api/admin/campaigns/preview', {
            'kind': 'service', 'serviceTopic': 'booking_update', 'subject': 'Your booking moved',
            'heading': 'Update', 'body': 'Your slot moved to 6 pm.'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('Booking update', response.data['html'])
        self.assertNotIn('#unsubscribe-preview', response.data['html'])

    def test_duplicate_keeps_the_kind(self):
        source = self.service_campaign()
        copy = self.admin_client.post(f'/api/admin/campaigns/{source.pk}/duplicate').data['campaign']
        self.assertEqual(copy['kind'], 'service')
        self.assertEqual(copy['serviceTopic'], 'service_announcement')

    def test_options_describe_kinds_topics_and_service_segments(self):
        data = self.admin_client.get('/api/admin/campaigns/options').data
        self.assertEqual([k['key'] for k in data['kinds']], ['promotional', 'service'])
        self.assertIn('booking_update', [t['key'] for t in data['serviceTopics']])
        self.assertNotIn('lapsed', data['serviceSegments'])


# ---------------------------------------------------------------------------
# Sending permissions and delivery
# ---------------------------------------------------------------------------

class KindSendTests(KindFixtures):

    def setUp(self):
        super().setUp()
        self.population()

    @mock.patch('campaigns.resend_client.requests.post')
    def test_nothing_is_sent_while_delivery_is_off(self, post):
        campaign = self.service_campaign()
        response = self.send_kind(campaign, confirmService=True)
        self.assertEqual(response.data['code'], 'DELIVERY_OFF')
        post.assert_not_called()

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_service_send_needs_its_own_confirmation(self, post):
        campaign = self.service_campaign()
        response = self.send_kind(campaign)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'CONFIRM_SERVICE_REQUIRED')
        post.assert_not_called()
        self.assertFalse(CampaignRecipient.objects.filter(campaign=campaign).exists())

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_service_with_offer_wording_is_refused(self, post):
        campaign = self.service_campaign(body='Flat 30% off all resorts this week!')
        response = self.send_kind(campaign, confirmService=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'INCOMPLETE')
        self.assertIn('kind', response.data['problems'])
        test = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/test', {}, format='json')
        self.assertEqual(test.status_code, 400)
        post.assert_not_called()

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_service_send_reaches_unsubscribed_customers_without_touching_consent(self, post):
        before = self.consent_snapshot()
        campaign = self.service_campaign()
        response = self.send_kind(campaign, confirmService=True)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'done')

        emails = post.call_args[1]['json']
        self.assertEqual(sorted(e['to'][0] for e in emails),
                         ['declined@mail.in', 'in@mail.in', 'never@mail.in', 'unsub@mail.in'])
        first = emails[0]
        self.assertEqual(first['from'], 'bookings@example.com')      # transactional sender
        self.assertNotIn('headers', first)                            # no List-Unsubscribe
        self.assertIn({'name': 'stream', 'value': 'service'}, first['tags'])
        self.assertNotIn('/api/email/unsubscribe', first['html'])
        self.assertIn('/account', first['html'])
        # Consent is exactly as it was: nobody opted in or out by this send.
        self.assertEqual(self.consent_snapshot(), before)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'sent')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_promotional_send_to_all_still_needs_opt_in(self, post):
        campaign = self.campaign(audience={'segment': 'all'})
        response = self.send_kind(campaign)
        self.assertEqual(response.status_code, 202, response.data)
        sender.process(campaign.pk, sleep=lambda s: None)
        emails = post.call_args[1]['json']
        self.assertEqual([e['to'][0] for e in emails], ['in@mail.in'])
        self.assertIn('List-Unsubscribe', emails[0]['headers'])
        self.assertIn({'name': 'stream', 'value': 'marketing'}, emails[0]['tags'])

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_offers_unsubscribe_after_queueing_does_not_stop_a_service_notice(self, post):
        campaign = self.service_campaign()
        self.send_kind(campaign, confirmService=True)
        opt_out(self.opted_in.pk, EmailPreference.Source.ACCOUNT)   # after queueing
        sender.process(campaign.pk, sleep=lambda s: None)
        row = CampaignRecipient.objects.get(campaign=campaign, email='in@mail.in')
        self.assertEqual(row.status, 'sent')

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_bounce_or_deactivation_after_queueing_skips_a_service_notice(self, post):
        campaign = self.service_campaign()
        self.send_kind(campaign, confirmService=True)
        EmailSuppression.objects.create(email='never@mail.in', reason='complaint')
        User.objects.filter(pk=self.declined.pk).update(is_active=False)
        sender.process(campaign.pk, sleep=lambda s: None)
        skipped = set(CampaignRecipient.objects.filter(campaign=campaign, status='skipped')
                      .values_list('email', flat=True))
        self.assertEqual(skipped, {'never@mail.in', 'declined@mail.in'})
        sent_to = {e['to'][0] for e in post.call_args[1]['json']}
        self.assertNotIn('never@mail.in', sent_to)
        self.assertNotIn('declined@mail.in', sent_to)

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_unsubscribe_link_still_only_affects_promotional_mail(self, post):
        token = sender.unsubscribe_token(self.opted_in.pk)
        response = APIClient().post(f'/api/email/unsubscribe?token={token}')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(EmailPreference.objects.get(user=self.opted_in).marketing_opt_in)
        self.assertNotIn('in@mail.in', [e for _u, e, _n in recipients({'segment': 'all'}, PROMOTIONAL)])
        self.assertIn('in@mail.in', [e for _u, e, _n in recipients({'segment': 'all'}, SERVICE)])

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_service_test_email_has_no_unsubscribe(self, post):
        campaign = self.service_campaign()
        response = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/test',
                                          {'to': ['qa@example.com']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        payload = post.call_args[1]['json']
        self.assertEqual(payload['to'], ['qa@example.com'])
        self.assertNotIn('headers', payload)
        self.assertTrue(payload['subject'].startswith('[TEST]'))

    @SERVICE_SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_only_admins_can_count_or_send_service_campaigns(self, post):
        campaign = self.service_campaign()
        customer_client = APIClient()
        customer_client.force_authenticate(self.opted_in)
        vendor_client = APIClient()
        vendor_client.force_login(self.vendor)
        for client in (APIClient(), customer_client, vendor_client):
            estimate_response = client.post('/api/admin/campaigns/audience',
                                            {'audience': {'segment': 'all'}, 'kind': 'service'},
                                            format='json')
            self.assertIn(estimate_response.status_code, (401, 403))
            send_response = client.post(f'/api/admin/campaigns/{campaign.pk}/send',
                                        {'confirm': True, 'confirmAll': True, 'confirmService': True,
                                         'expectedCount': 4, 'version': 1}, format='json')
            self.assertIn(send_response.status_code, (401, 403))
        post.assert_not_called()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'draft')
