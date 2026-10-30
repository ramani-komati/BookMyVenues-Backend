"""
Email campaigns. Every test runs with no real provider: CAMPAIGN_DELIVERY is
'off' by default in settings_test, and the tests that exercise sending patch
`requests.post` inside campaigns.resend_client — nothing leaves the machine.
"""
import base64
import datetime
import hashlib
import hmac
import json
import time
import uuid
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from bookings.models import Booking
from venues.models import Listing

from . import content, sender
from .audience import estimate, mask_email, segment_queryset
from .models import (
    Campaign,
    CampaignRecipient,
    EmailPreference,
    EmailSuppression,
    ResendWebhookEvent,
)
from .public_views import verify_svix

SEND = override_settings(CAMPAIGN_DELIVERY='resend', RESEND_API_KEY='re_fake_key',
                         RESEND_FROM='hello@example.com', CAMPAIGN_FROM='offers@example.com')
DRY = override_settings(CAMPAIGN_DELIVERY='dry-run')
WEBHOOK_SECRET = 'whsec_' + base64.b64encode(b'super-secret-key-for-tests').decode()


def fake_response(status=200, body=None, headers=None):
    response = mock.Mock()
    response.status_code = status
    response.json.return_value = body if body is not None else {}
    response.headers = headers or {}
    return response


def batch_ok(*args, **kwargs):
    payload = kwargs.get('json') or []
    if isinstance(payload, list):
        return fake_response(200, {'data': [{'id': f're_{uuid.uuid4().hex[:8]}'} for _ in payload]})
    return fake_response(200, {'id': f're_{uuid.uuid4().hex[:8]}'})


class CampaignTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            phone='9990000201', name='Anita Admin', email='anita@admin.in',
            role=User.Role.ADMIN, password='pw')
        self.vendor = User.objects.create_user(phone='9990000202', name='Vik', role=User.Role.VENDOR)
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)
        self._phone = 9100000000

    # -- fixtures -----------------------------------------------------------
    def customer(self, name='Asha Rao', email=None, opted_in=True, **extra):
        self._phone += 1
        user = User.objects.create_user(
            phone=str(self._phone), name=name,
            email=email if email is not None else f'c{self._phone}@mail.in',
            is_customer=True, **extra)
        if opted_in is not None:
            EmailPreference.objects.create(user=user, marketing_opt_in=opted_in,
                                           consented_at=timezone.now() if opted_in else None)
        return user

    def listing(self, name='Grand Hall', state='Telangana', city='Hanamkonda', district='Hanumakonda',
                category='Party hall'):
        return Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug=f'{name.lower().replace(" ", "-")}-{uuid.uuid4().hex[:4]}',
            record={'name': name, 'state': state, 'city': city, 'district': district},
            name=name, category=category, status='live')

    def book(self, user, listing, status='confirmed', days_ago=1):
        booking = Booking.objects.create(
            listing=listing, user=user, venue_name=listing.name, category=listing.category,
            date=timezone.now().date(), slots=['10:00 – 11:00'], amount=900, status=status,
            razorpay_payment_id='pay_x')
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - datetime.timedelta(days=days_ago))
        return booking

    def campaign(self, **fields):
        defaults = dict(name='Diwali offers', subject='Big Diwali savings', heading='Hi {{first_name}}!',
                        body='Book now and **save 20%**.\n\n- Party halls\n- Resorts',
                        cta_label='Book now', cta_url='https://thebookmyvenues.in/explore',
                        images=[{'url': 'https://cdn.example.com/a.jpg', 'alt': 'Banner',
                                 'link': '', 'caption': ''}],
                        audience={'segment': 'booked'}, created_by=self.admin)
        defaults.update(fields)
        return Campaign.objects.create(**defaults)

    def send(self, campaign, **body):
        count = segment_queryset(campaign.audience).count()
        payload = {'confirm': True, 'expectedCount': count, 'version': campaign.version, **body}
        if (campaign.audience or {}).get('segment') == 'all':
            payload.setdefault('confirmAll', True)
        return self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/send', payload, format='json')


# ---------------------------------------------------------------------------
# Consent and audience
# ---------------------------------------------------------------------------

class AudienceTests(CampaignTestBase):

    def test_only_opted_in_customers_with_an_email_are_eligible(self):
        yes = self.customer('Yes')
        self.customer('Never asked', opted_in=None)
        self.customer('Said no', opted_in=False)
        self.customer('No email', email='', opted_in=True)
        self.customer('Blocked', is_active=False)
        vendor_only = User.objects.create_user(phone='9990000299', email='v@x.in', role=User.Role.VENDOR)
        EmailPreference.objects.create(user=vendor_only, marketing_opt_in=True)

        ids = list(segment_queryset({'segment': 'all'}).values_list('pk', flat=True))
        self.assertEqual(ids, [yes.pk])

    def test_suppressed_addresses_are_excluded_case_insensitively(self):
        user = self.customer(email='Bounced@Mail.in')
        EmailSuppression.objects.create(email='bounced@mail.in', reason='bounce')
        self.assertFalse(segment_queryset({'segment': 'all'}).filter(pk=user.pk).exists())
        self.assertEqual(estimate({'segment': 'all'})['suppressed'], 1)

    def test_booked_not_booked_lapsed_category_and_location_segments(self):
        hall = self.listing()
        resort = self.listing('Lake Resort', state='Karnataka', city='Bengaluru', district='Bengaluru Urban',
                              category='Resort')
        recent = self.customer('Recent')
        old = self.customer('Old')
        never = self.customer('Never')
        cancelled = self.customer('Cancelled only')
        self.book(recent, hall, days_ago=3)
        self.book(old, resort, days_ago=200)
        self.book(cancelled, hall, status='cancelled')

        def ids(audience):
            return set(segment_queryset(audience).values_list('pk', flat=True))

        self.assertEqual(ids({'segment': 'booked'}), {recent.pk, old.pk})
        self.assertEqual(ids({'segment': 'booked', 'days': 30}), {recent.pk})
        self.assertEqual(ids({'segment': 'not_booked'}), {never.pk, cancelled.pk})
        self.assertEqual(ids({'segment': 'lapsed', 'days': 90}), {old.pk})
        self.assertEqual(ids({'segment': 'category', 'category': 'resort'}), {old.pk})
        self.assertEqual(ids({'segment': 'location', 'state': 'Telangana'}), {recent.pk})
        self.assertEqual(ids({'segment': 'location', 'state': 'Karnataka', 'city': 'bengaluru urban'}), {old.pk})
        self.assertEqual(ids({'segment': 'location', 'state': 'Karnataka', 'city': 'Mysuru'}), set())

    def test_estimate_endpoint_returns_counts_never_addresses(self):
        self.customer('One')
        self.customer('Two')
        response = self.admin_client.post('/api/admin/campaigns/audience',
                                          {'audience': {'segment': 'all'}}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['count'], 2)
        self.assertNotIn('@', json.dumps(response.data))

    def test_options_list_states_and_cities_from_booked_venues(self):
        self.book(self.customer(), self.listing())
        data = self.admin_client.get('/api/admin/campaigns/options').data
        self.assertEqual(data['locations'][0]['state'], 'Telangana')
        self.assertIn('Hanamkonda', data['locations'][0]['cities'])
        self.assertEqual(data['categories'], ['Party hall'])
        self.assertEqual(data['delivery']['mode'], 'off')

    def test_mask_email(self):
        self.assertEqual(mask_email('ravi@gmail.com'), 'r***@gmail.com')


# ---------------------------------------------------------------------------
# Admin API: auth, drafts, preview
# ---------------------------------------------------------------------------

class AdminApiTests(CampaignTestBase):

    def test_every_campaign_route_is_admin_only(self):
        campaign = self.campaign()
        customer = self.customer()
        routes = [
            ('get', '/api/admin/campaigns'), ('post', '/api/admin/campaigns'),
            ('get', '/api/admin/campaigns/options'), ('post', '/api/admin/campaigns/audience'),
            ('post', '/api/admin/campaigns/preview'), ('get', '/api/admin/campaigns/images'),
            ('get', f'/api/admin/campaigns/{campaign.pk}'),
            ('patch', f'/api/admin/campaigns/{campaign.pk}'),
            ('post', f'/api/admin/campaigns/{campaign.pk}/test'),
            ('post', f'/api/admin/campaigns/{campaign.pk}/send'),
            ('get', f'/api/admin/campaigns/{campaign.pk}/recipients'),
        ]
        anonymous = APIClient()
        signed_in_customer = APIClient()
        signed_in_customer.force_login(customer)
        for method, url in routes:
            for client in (anonymous, signed_in_customer):
                response = getattr(client, method)(url, {}, format='json')
                self.assertIn(response.status_code, (401, 403), f'{method} {url}')

    def test_create_save_and_version_conflict(self):
        created = self.admin_client.post('/api/admin/campaigns', {
            'name': 'Monsoon', 'subject': 'Rainy day deals', 'audience': {'segment': 'all'},
        }, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        row = created.data['campaign']
        self.assertEqual((row['status'], row['version']), ('draft', 1))

        saved = self.admin_client.patch(f'/api/admin/campaigns/{row["id"]}', {
            'version': 1, 'heading': 'Hello', 'body': 'Text',
            'images': [{'url': 'https://cdn.example.com/x.jpg', 'alt': 'x'}],
            'ctaLabel': 'Explore', 'ctaUrl': '/explore',
        }, format='json')
        self.assertEqual(saved.status_code, 200, saved.data)
        self.assertEqual(saved.data['campaign']['version'], 2)
        self.assertEqual(saved.data['campaign']['ctaUrl'], 'https://thebookmyvenues.in/explore')

        stale = self.admin_client.patch(f'/api/admin/campaigns/{row["id"]}',
                                        {'version': 1, 'heading': 'Lost update'}, format='json')
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data['code'], 'STALE')

    def test_unsafe_links_are_refused(self):
        campaign = self.campaign()
        for bad in ('javascript:alert(1)', 'http://plain.example.com', '//evil.example.com'):
            response = self.admin_client.patch(f'/api/admin/campaigns/{campaign.pk}',
                                               {'version': campaign.version, 'ctaUrl': bad}, format='json')
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.data['field'], 'ctaUrl')

    def test_preview_escapes_admin_text_and_formats_it(self):
        response = self.admin_client.post('/api/admin/campaigns/preview', {
            'subject': 'Hi {{first_name}}', 'heading': '<script>x</script> **Deal**',
            'body': 'Line one\nline two\n\n- a\n- b', 'ctaLabel': 'Go', 'ctaUrl': 'https://thebookmyvenues.in/x',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        html = response.data['html']
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertIn('<strong>Deal</strong>', html)
        self.assertIn('Line one<br>line two', html)
        self.assertIn('<li', html)
        self.assertIn('utm_source=email', html)
        self.assertEqual(response.data['subject'], 'Hi Anita')

    def test_sent_campaigns_cannot_be_edited_or_deleted(self):
        campaign = self.campaign(status=Campaign.Status.SENT)
        edit = self.admin_client.patch(f'/api/admin/campaigns/{campaign.pk}',
                                       {'version': campaign.version, 'name': 'x'}, format='json')
        delete = self.admin_client.delete(f'/api/admin/campaigns/{campaign.pk}')
        self.assertEqual((edit.status_code, delete.status_code), (409, 409))

    def test_duplicate_makes_a_new_draft(self):
        source = self.campaign(status=Campaign.Status.SENT)
        response = self.admin_client.post(f'/api/admin/campaigns/{source.pk}/duplicate')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['campaign']['status'], 'draft')
        self.assertEqual(response.data['campaign']['subject'], source.subject)

    @mock.patch('venues.storage.upload_photo', return_value='https://cdn.example.com/campaigns/x.png')
    def test_image_upload_checks_the_file(self, store):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from adminpanel.test_homepage import png
        ok = self.admin_client.post('/api/admin/campaigns/images',
                                    {'image': SimpleUploadedFile('a.png', png(1200, 600), 'image/png')},
                                    HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(ok.status_code, 201, ok.data)
        self.assertTrue(store.call_args[0][0].startswith('campaigns/'))
        small = self.admin_client.post('/api/admin/campaigns/images',
                                       {'image': SimpleUploadedFile('s.png', png(120, 60), 'image/png')},
                                       HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(small.status_code, 400)
        no_header = self.admin_client.post('/api/admin/campaigns/images',
                                           {'image': SimpleUploadedFile('a.png', png(1200, 600), 'image/png')})
        self.assertEqual(no_header.status_code, 400)


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

class SendTests(CampaignTestBase):

    def setUp(self):
        super().setUp()
        self.hall = self.listing()
        self.customers = [self.customer(f'Customer {i}') for i in range(3)]
        for user in self.customers:
            self.book(user, self.hall)

    @mock.patch('campaigns.resend_client.requests.post')
    def test_nothing_is_sent_while_delivery_is_off(self, post):
        campaign = self.campaign()
        response = self.send(campaign)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'DELIVERY_OFF')
        test = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/test', {}, format='json')
        self.assertEqual(test.data['code'], 'DELIVERY_OFF')
        post.assert_not_called()
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'draft')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_send_batches_through_resend_with_unsubscribe_headers(self, post):
        campaign = self.campaign()
        response = self.send(campaign)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data['campaign']['status'], 'queued')
        post.assert_not_called()                      # the worker sends, not the request

        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'done')
        self.assertEqual(post.call_count, 1)
        url = post.call_args[0][0]
        kwargs = post.call_args[1]
        self.assertEqual(url, 'https://api.resend.com/emails/batch')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer re_fake_key')
        self.assertTrue(kwargs['headers']['Idempotency-Key'].startswith('bmv-campaign/'))
        emails = kwargs['json']
        self.assertEqual(len(emails), 3)
        first = emails[0]
        self.assertEqual(first['from'], 'offers@example.com')
        self.assertEqual(len(first['to']), 1)
        self.assertIn('/api/email/unsubscribe?token=', first['headers']['List-Unsubscribe'])
        self.assertEqual(first['headers']['List-Unsubscribe-Post'], 'List-Unsubscribe=One-Click')
        self.assertIn({'name': 'stream', 'value': 'marketing'}, first['tags'])
        self.assertIn('Hi Customer!', first['html'])          # {{first_name}}
        self.assertIn('Unsubscribe', first['text'])

        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'sent')
        self.assertEqual(campaign.recipients.filter(status='sent').exclude(resend_id='').count(), 3)

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_a_campaign_can_never_be_sent_twice(self, post):
        campaign = self.campaign()
        self.assertEqual(self.send(campaign).status_code, 202)
        again = self.send(Campaign.objects.get(pk=campaign.pk))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data['code'], 'ALREADY_SENT')
        sender.process(campaign.pk, sleep=lambda s: None)
        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'busy')   # finished
        self.assertEqual(post.call_count, 1)
        self.assertEqual(CampaignRecipient.objects.filter(campaign=campaign).count(), 3)

    @SEND
    def test_a_live_lease_blocks_a_second_worker(self):
        campaign = self.campaign()
        self.send(campaign)
        Campaign.objects.filter(pk=campaign.pk).update(
            status='sending', lease_token='other', lease_expires_at=timezone.now() + datetime.timedelta(minutes=5))
        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'busy')

    @SEND
    def test_send_requires_confirmation_and_a_current_count(self):
        campaign = self.campaign()
        no_confirm = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/send',
                                            {'expectedCount': 3, 'version': 1}, format='json')
        self.assertEqual(no_confirm.data['code'], 'CONFIRM_REQUIRED')
        wrong = self.send(campaign, expectedCount=99)
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual((wrong.data['code'], wrong.data['count']), ('COUNT_CHANGED', 3))

        everyone = self.campaign(audience={'segment': 'all'})
        response = self.admin_client.post(f'/api/admin/campaigns/{everyone.pk}/send',
                                          {'confirm': True, 'expectedCount': 3, 'version': 1}, format='json')
        self.assertEqual(response.data['code'], 'CONFIRM_ALL_REQUIRED')
        self.assertEqual(Campaign.objects.get(pk=everyone.pk).status, 'draft')

    @SEND
    def test_incomplete_or_empty_campaigns_are_refused(self):
        incomplete = self.campaign(subject='')
        self.assertEqual(self.send(incomplete).data['code'], 'INCOMPLETE')
        nobody = self.campaign(audience={'segment': 'event_buyers'})
        self.assertEqual(self.send(nobody).data['code'], 'NO_RECIPIENTS')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_someone_who_unsubscribes_after_queueing_is_skipped(self, post):
        campaign = self.campaign()
        self.send(campaign)
        EmailPreference.objects.filter(user=self.customers[0]).update(marketing_opt_in=False)
        sender.process(campaign.pk, sleep=lambda s: None)
        self.assertEqual(len(post.call_args[1]['json']), 2)
        row = CampaignRecipient.objects.get(campaign=campaign, user=self.customers[0])
        self.assertEqual(row.status, 'skipped')
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).status, 'sent')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post')
    def test_transient_errors_retry_with_the_same_idempotency_key(self, post):
        post.side_effect = [fake_response(503), fake_response(429, headers={'retry-after': '1'}),
                            batch_ok(json=[{}, {}, {}])]
        campaign = self.campaign()
        self.send(campaign)
        with mock.patch('campaigns.sender.time.sleep') as quick_sleep:
            self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'done')
        self.assertEqual(quick_sleep.call_args_list[1], mock.call(1.0))   # honours Retry-After
        keys = {c[1]['headers']['Idempotency-Key'] for c in post.call_args_list}
        self.assertEqual(len(keys), 1)                    # one batch, one key
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).status, 'sent')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', return_value=fake_response(500))
    @mock.patch('campaigns.sender.time.sleep')
    def test_persistent_outage_defers_then_fails_after_the_last_attempt(self, _sleep, post):
        campaign = self.campaign()
        self.send(campaign)
        # Each worker run hits the outage; rows go back with a growing wait.
        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None, max_wait_seconds=0), 'waiting')
        row = CampaignRecipient.objects.filter(campaign=campaign).first()
        self.assertEqual((row.status, row.attempts), ('pending', 1))
        for _ in range(sender.MAX_ATTEMPTS):
            CampaignRecipient.objects.filter(campaign=campaign).update(next_attempt_at=None)
            sender.process(campaign.pk, sleep=lambda s: None, max_wait_seconds=0)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'failed')
        self.assertTrue(all(r.status == 'failed' for r in campaign.recipients.all()))

    @SEND
    @mock.patch('campaigns.resend_client.requests.post')
    def test_a_rejected_batch_is_retried_one_by_one(self, post):
        def answer(url, json=None, headers=None, timeout=None):
            if url.endswith('/batch'):
                return fake_response(422, {'name': 'validation_error', 'message': 'Invalid `to` x@y.z'})
            if json['to'][0] == self.customers[1].email.lower():
                return fake_response(422, {'name': 'validation_error', 'message': 'bad x@y.z'})
            return fake_response(200, {'id': 're_single'})
        post.side_effect = answer
        campaign = self.campaign()
        self.send(campaign)
        with mock.patch('campaigns.sender.time.sleep'):
            sender.process(campaign.pk, sleep=lambda s: None)
        campaign.refresh_from_db()
        self.assertEqual(campaign.status, 'partial')
        failed = campaign.recipients.get(status='failed')
        self.assertEqual(failed.user_id, self.customers[1].pk)
        self.assertNotIn('x@y.z', failed.error)              # addresses masked in stored errors

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_a_batch_left_mid_call_is_resent_under_its_original_key(self, post):
        campaign = self.campaign()
        self.send(campaign)
        CampaignRecipient.objects.filter(campaign=campaign).update(status='sending', batch_key='abc123')
        Campaign.objects.filter(pk=campaign.pk).update(status='sending')
        sender.process(campaign.pk, sleep=lambda s: None)
        self.assertEqual(post.call_args[1]['headers']['Idempotency-Key'], 'bmv-campaign/abc123')

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_cancel_stops_the_rest(self, post):
        campaign = self.campaign()
        self.send(campaign)
        response = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sender.process(campaign.pk, sleep=lambda s: None), 'busy')
        post.assert_not_called()
        self.assertEqual(campaign.recipients.filter(status='skipped').count(), 3)

    @DRY
    @mock.patch('campaigns.resend_client.requests.post')
    def test_dry_run_runs_the_pipeline_without_calling_resend(self, post):
        campaign = self.campaign()
        self.assertEqual(self.send(campaign).status_code, 202)
        sender.process(campaign.pk, sleep=lambda s: None)
        campaign.refresh_from_db()
        self.assertTrue(campaign.dry_run)
        self.assertEqual(campaign.status, 'sent')
        post.assert_not_called()

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_test_email_goes_only_to_the_typed_addresses(self, post):
        campaign = self.campaign()
        response = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/test',
                                          {'to': ['qa@example.com']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        payload = post.call_args[1]['json']
        self.assertEqual(payload['to'], ['qa@example.com'])
        self.assertTrue(payload['subject'].startswith('[TEST] '))
        self.assertEqual(CampaignRecipient.objects.count(), 0)
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).status, 'draft')
        too_many = self.admin_client.post(f'/api/admin/campaigns/{campaign.pk}/test',
                                          {'to': [f'a{i}@x.in' for i in range(6)]}, format='json')
        self.assertEqual(too_many.status_code, 400)

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_history_reports_stats_and_masks_addresses(self, post):
        campaign = self.campaign()
        self.send(campaign)
        sender.process(campaign.pk, sleep=lambda s: None)
        listing = self.admin_client.get('/api/admin/campaigns').data
        stats = listing['campaigns'][0]['stats']
        self.assertEqual((stats['recipients'], stats['sent'], stats['failed']), (3, 3, 0))
        recipients = self.admin_client.get(f'/api/admin/campaigns/{campaign.pk}/recipients').data
        self.assertEqual(recipients['total'], 3)
        self.assertTrue(all('***@' in r['email'] for r in recipients['recipients']))

    @SEND
    @mock.patch('campaigns.resend_client.requests.post', side_effect=batch_ok)
    def test_management_command_resumes_a_stalled_campaign(self, post):
        campaign = self.campaign()
        self.send(campaign)
        with mock.patch('campaigns.sender.time.sleep'):
            call_command('send_email_campaigns', stdout=mock.Mock())
        self.assertEqual(Campaign.objects.get(pk=campaign.pk).status, 'sent')


# ---------------------------------------------------------------------------
# Unsubscribe, customer preference, webhook
# ---------------------------------------------------------------------------

class UnsubscribeAndPreferenceTests(CampaignTestBase):

    def test_customer_opts_in_and_out_from_their_account(self):
        from adminpanel.test_homepage import jwt_client
        user = self.customer(opted_in=None)
        client = jwt_client(user)
        self.assertFalse(client.get('/api/users/me/email-preferences').data['preferences']['marketingEmails'])
        on = client.patch('/api/users/me/email-preferences', {'marketingEmails': True}, format='json')
        self.assertTrue(on.data['preferences']['marketingEmails'])
        pref = EmailPreference.objects.get(user=user)
        self.assertEqual(pref.source, 'account')
        self.assertIsNotNone(pref.consented_at)
        off = client.patch('/api/users/me/email-preferences', {'marketingEmails': False}, format='json')
        self.assertFalse(off.data['preferences']['marketingEmails'])
        self.assertEqual(APIClient().get('/api/users/me/email-preferences').status_code, 401)

    def test_opting_in_needs_an_email(self):
        from adminpanel.test_homepage import jwt_client
        user = self.customer(email='', opted_in=None)
        response = jwt_client(user).patch('/api/users/me/email-preferences',
                                          {'marketingEmails': True}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_get_only_asks_and_post_unsubscribes(self):
        user = self.customer()
        campaign = self.campaign()
        row = CampaignRecipient.objects.create(campaign=campaign, user=user, email=user.email)
        token = sender.unsubscribe_token(user.pk, campaign.pk)
        page = self.client.get('/api/email/unsubscribe', {'token': token})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Unsubscribe from offers?')
        self.assertTrue(EmailPreference.objects.get(user=user).marketing_opt_in)   # a GET changes nothing

        done = self.client.post(f'/api/email/unsubscribe?token={token}')
        self.assertContains(done, 'unsubscribed')
        pref = EmailPreference.objects.get(user=user)
        self.assertFalse(pref.marketing_opt_in)
        self.assertEqual(pref.source, 'unsubscribe_link')
        row.refresh_from_db()
        self.assertIsNotNone(row.unsubscribed_at)

    def test_one_click_unsubscribe_from_a_mail_app(self):
        user = self.customer()
        token = sender.unsubscribe_token(user.pk)
        response = self.client.post(f'/api/email/unsubscribe?token={token}',
                                    {'List-Unsubscribe': 'One-Click'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(EmailPreference.objects.get(user=user).source, 'one_click')

    def test_tampered_and_test_tokens_change_nothing(self):
        user = self.customer()
        bad = self.client.post('/api/email/unsubscribe?token=' + sender.unsubscribe_token(user.pk) + 'x')
        self.assertEqual(bad.status_code, 400)
        test = self.client.post('/api/email/unsubscribe?token=' + sender.unsubscribe_token(user.pk, test=True))
        self.assertContains(test, 'test email')
        self.assertTrue(EmailPreference.objects.get(user=user).marketing_opt_in)

    def test_tokens_are_deterministic_and_carry_no_address(self):
        user = self.customer(email='secret@mail.in')
        token = sender.unsubscribe_token(user.pk, 'c1')
        self.assertEqual(token, sender.unsubscribe_token(user.pk, 'c1'))
        self.assertNotIn(b'secret', base64.urlsafe_b64decode(token.split(':')[0] + '=='))

    def test_unsubscribing_never_touches_transactional_mail(self):
        """OTP / booking mail never consults the marketing preference."""
        import inspect

        from accounts import otp
        from bookings import notifications
        for module in (otp, notifications):
            self.assertNotIn('EmailPreference', inspect.getsource(module))
            self.assertNotIn('marketing', inspect.getsource(module))


def signed(body, secret=WEBHOOK_SECRET, msg_id=None, stamp=None):
    msg_id = msg_id or f'msg_{uuid.uuid4().hex}'
    stamp = str(stamp or int(time.time()))
    raw = json.dumps(body).encode()
    key = base64.b64decode(secret.split('_', 1)[1])
    sig = base64.b64encode(hmac.new(key, f'{msg_id}.{stamp}.'.encode() + raw, hashlib.sha256).digest()).decode()
    return raw, {'HTTP_SVIX_ID': msg_id, 'HTTP_SVIX_TIMESTAMP': stamp, 'HTTP_SVIX_SIGNATURE': f'v1,{sig}'}


@override_settings(RESEND_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTests(CampaignTestBase):

    def setUp(self):
        super().setUp()
        self.user = self.customer()
        self.campaign_obj = self.campaign(status=Campaign.Status.SENT)
        self.row = CampaignRecipient.objects.create(
            campaign=self.campaign_obj, user=self.user, email=self.user.email,
            status='sent', resend_id='re_123')

    def post(self, event_type, data=None, **sign):
        raw, headers = signed({'type': event_type, 'data': {'email_id': 're_123', **(data or {})}}, **sign)
        return self.client.post('/api/email/webhooks/resend', raw, content_type='application/json', **headers)

    def test_delivery_open_and_click_are_recorded(self):
        self.assertEqual(self.post('email.delivered').status_code, 200)
        self.post('email.clicked')
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'delivered')
        self.assertIsNotNone(self.row.clicked_at)
        self.assertIsNotNone(self.row.opened_at)

    def test_hard_bounce_suppresses_the_address(self):
        self.post('email.bounced', {'bounce': {'type': 'Permanent', 'message': 'No such user'}})
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'bounced')
        self.assertTrue(EmailSuppression.objects.filter(email=self.user.email.lower()).exists())
        self.post('email.delivered')                       # a late event never undoes it
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'bounced')

    def test_soft_bounce_does_not_suppress(self):
        self.post('email.bounced', {'bounce': {'type': 'Transient', 'message': 'Mailbox full'}})
        self.assertFalse(EmailSuppression.objects.exists())

    def test_complaint_unsubscribes_and_suppresses(self):
        self.post('email.complained')
        self.assertFalse(EmailPreference.objects.get(user=self.user).marketing_opt_in)
        self.assertTrue(EmailSuppression.objects.filter(reason='complaint').exists())

    def test_bad_signature_old_timestamp_and_missing_secret_are_refused(self):
        raw, headers = signed({'type': 'email.delivered', 'data': {'email_id': 're_123'}},
                              secret='whsec_' + base64.b64encode(b'wrong').decode())
        self.assertEqual(self.client.post('/api/email/webhooks/resend', raw,
                                          content_type='application/json', **headers).status_code, 401)
        self.assertEqual(self.post('email.delivered', stamp=int(time.time()) - 3600).status_code, 401)
        with override_settings(RESEND_WEBHOOK_SECRET=''):
            self.assertEqual(self.post('email.delivered').status_code, 503)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'sent')

    def test_redelivered_events_apply_once_and_unknown_ids_are_ignored(self):
        self.post('email.delivered', msg_id='msg_same')
        self.post('email.delivered', msg_id='msg_same')
        self.assertEqual(ResendWebhookEvent.objects.count(), 1)
        raw, headers = signed({'type': 'email.delivered', 'data': {'email_id': 're_transactional'}})
        response = self.client.post('/api/email/webhooks/resend', raw,
                                    content_type='application/json', **headers)
        self.assertEqual(response.status_code, 200)

    def test_verify_svix_rejects_missing_headers(self):
        self.assertFalse(verify_svix(WEBHOOK_SECRET, {}, b'{}'))


class ContentTests(TestCase):

    def test_body_blocks(self):
        blocks = content.body_blocks('One\nTwo\n\n- a\n- b\n\n**x** <y>')
        self.assertEqual(blocks[0], ('p', 'One<br>Two'))
        self.assertEqual(blocks[1][0], 'ul')
        self.assertEqual(blocks[2], ('p', '<strong>x</strong> &lt;y&gt;'))

    def test_tracking_only_on_our_site(self):
        campaign = Campaign(name='Diwali 2026')
        self.assertIn('utm_campaign=diwali-2026', content.tracked('https://thebookmyvenues.in/x', campaign))
        self.assertEqual(content.tracked('https://other.example.com/x', campaign), 'https://other.example.com/x')

    def test_personalize_escapes_the_name(self):
        campaign = Campaign(name='x', subject='Hi {{first_name}}', heading='Hey {{first_name}}', body='b')
        subject, html, text = content.personalize(
            content.render_template(campaign), name='<b>Ravi</b> K', unsubscribe_url='https://api.x/u?token=a&b')
        self.assertEqual(subject, 'Hi <b>Ravi</b>')
        self.assertIn('Hey &lt;b&gt;Ravi&lt;/b&gt;', html)
        self.assertIn('https://api.x/u?token=a&amp;b', html)
        self.assertIn('https://api.x/u?token=a&b', text)
