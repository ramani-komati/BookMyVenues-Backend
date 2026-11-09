"""
Marketing consent at sign-up: the new-customer profile step offers an
optional, UNTICKED box. Ticking it is the customer's own opt-in (recorded as
source 'registration'); leaving it alone means no promotional email. It can
never overwrite a decision the customer already made — an existing
preference (e.g. an earlier unsubscribe) is only changed as an ordinary
account change, by the customer, and is recorded as such.
"""
from django.test import TestCase

from campaigns import audience
from campaigns.models import EmailPreference
from events.tests import auth, make_customer

URL = '/api/users/me/email-preferences'


class RegistrationConsentTests(TestCase):

    def setUp(self):
        self.asha = make_customer('9100000001', 'Asha')

    def eligible_ids(self):
        return {row[0] for row in audience.recipients({'segment': 'all'})}

    def test_ticking_the_signup_box_records_a_registration_opt_in(self):
        response = auth(self.asha).patch(URL, {'marketingEmails': True, 'source': 'registration'}, format='json')
        self.assertEqual(response.status_code, 200)
        pref = EmailPreference.objects.get(user=self.asha)
        self.assertTrue(pref.marketing_opt_in)
        self.assertEqual(pref.source, 'registration')
        self.assertIsNotNone(pref.consented_at)
        self.assertIn(self.asha.id, self.eligible_ids())
        body = response.data['preferences']
        self.assertEqual(body['source'], 'registration')
        self.assertIsNotNone(body['consentedAt'])

    def test_a_new_customer_who_does_not_tick_it_gets_no_promotions(self):
        # The sign-up step sends nothing when the box is left unticked.
        self.assertFalse(EmailPreference.objects.filter(user=self.asha).exists())
        self.assertNotIn(self.asha.id, self.eligible_ids())
        body = auth(self.asha).get(URL).data['preferences']
        self.assertEqual((body['marketingEmails'], body['consentedAt'], body['source']), (False, None, None))

    def test_registration_never_silently_rewrites_an_earlier_decision(self):
        EmailPreference.objects.create(user=self.asha, marketing_opt_in=False, source='unsubscribe_link')
        auth(self.asha).patch(URL, {'marketingEmails': True, 'source': 'registration'}, format='json')
        pref = EmailPreference.objects.get(user=self.asha)
        # The customer's own explicit choice still applies, but is recorded
        # as an account change — not as if it were a first sign-up.
        self.assertEqual(pref.source, 'account')

    def test_an_unknown_source_is_treated_as_the_account_toggle(self):
        auth(self.asha).patch(URL, {'marketingEmails': True, 'source': 'admin'}, format='json')
        self.assertEqual(EmailPreference.objects.get(user=self.asha).source, 'account')

    def test_opt_in_without_an_email_address_is_refused(self):
        self.asha.email = None
        self.asha.save(update_fields=['email'])
        response = auth(self.asha).patch(URL, {'marketingEmails': True, 'source': 'registration'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(EmailPreference.objects.filter(user=self.asha, marketing_opt_in=True).exists())

    def test_unsubscribing_after_a_signup_opt_in_removes_them_from_audiences(self):
        client = auth(self.asha)
        client.patch(URL, {'marketingEmails': True, 'source': 'registration'}, format='json')
        self.assertIn(self.asha.id, self.eligible_ids())
        client.patch(URL, {'marketingEmails': False}, format='json')
        self.assertNotIn(self.asha.id, self.eligible_ids())
        self.assertIsNotNone(EmailPreference.objects.get(user=self.asha).unsubscribed_at)
