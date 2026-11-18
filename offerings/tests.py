"""
Fitness memberships, resort packages and big screen surprises.

  * publish validates the record block and syncs it into the tables; a
    removed plan / package / show is archived, never deleted
  * the draft submit gate demands a plan / package / show
  * checkout prices from the server's rows, refuses tampered amounts, and
    goes through the existing Razorpay order → verify path
  * capacity: a resort slot and a cinema screen + show time can never be
    sold past their limit, even when a hold expires mid-payment
  * memberships: validity dates, joining fee only once, visits, own list
  * offering bookings never block ordinary slot bookings
"""
import datetime
import hashlib
import hmac
import uuid
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from venues.completion import compute_completion
from venues.models import Listing, VenueDraft

from .checkout import add_duration
from .models import FitnessPlan, Membership, OfferingProfile, ResortPackage, SurpriseShow
from .schema import OfferingError, clean_record

TOMORROW = today_ist() + datetime.timedelta(days=1)
IN_A_WEEK = today_ist() + datetime.timedelta(days=7)


def plan(**over):
    return {'id': 'p-month', 'kind': 'monthly', 'name': 'Monthly Membership', 'enabled': True,
            'price': '1500', 'durationValue': '1', 'durationUnit': 'month', 'startRule': 'purchase',
            'expiryRule': 'duration', 'visits': '', 'facilities': ['Locker Room'],
            'trialAvailable': True, 'trialPrice': '0', 'joiningFee': '500', **over}


def package(**over):
    return {'id': 'pkg-family', 'name': 'Family Day Out', 'kind': 'day', 'duration': 'half_day',
            'pricing': 'per_person', 'price': '800', 'childPrice': '400', 'minPersons': '2',
            'maxPersons': '10', 'capacity': '12', 'slots': [{'start': '09:00', 'end': '13:00'}],
            'inclusions': ['Swimming Pool', 'Lunch'], 'otherInclusion': 'Welcome drink', 'enabled': True, **over}


def show(**over):
    return {'id': 'show-1', 'movie': 'Leo', 'language': 'Telugu', 'screen': '1',
            'times': ['18:30', '21:30'], 'enabled': True, **over}


def record(kind, block, name='Venue', **detail):
    venue_type = {'fitness': 'Gym', 'resort': 'Resort', 'surprise': 'Big Screen Surprise'}[kind]
    category = {'fitness': 'Gym', 'resort': 'Resort', 'surprise': 'Big screen surprise'}[kind]
    return {'name': name, 'category': category, 'price': '1', 'location': 'Hanamkonda',
            'detail': {'primaryCategory': venue_type, kind: block, **detail}}


SURPRISE = {'screens': '2', 'price': '999', 'occasionPrices': {'Proposal': '1499'},
            'maxBookingsPerShow': '1', 'minLeadHours': '0', 'allowPhoto': True,
            'availableDays': [], 'shows': [show()]}


class SchemaTests(SimpleTestCase):
    def test_fitness_block_is_normalised(self):
        cleaned = clean_record(record('fitness', {'plans': [plan()], 'operatingDays': ['Mon', 'tue']}))
        block = cleaned['detail']['fitness']
        self.assertEqual(block['plans'][0]['price'], 1500)
        self.assertEqual(block['plans'][0]['joiningFee'], 500)
        self.assertIsNone(block['plans'][0]['visits'])
        self.assertEqual(block['operatingDays'], ['mon', 'tue'])

    def test_bad_values_are_refused(self):
        for bad in (plan(price='-5'), plan(price='abc'), plan(durationUnit='year'),
                    plan(expiryRule='visits_or_duration', visits=''), plan(id='')):
            with self.assertRaises(OfferingError, msg=bad):
                clean_record(record('fitness', {'plans': [bad]}))
        with self.assertRaises(OfferingError):
            clean_record(record('fitness', {'plans': [plan(), plan()]}))      # repeated id

    def test_a_disabled_plan_may_be_half_filled(self):
        cleaned = clean_record(record('fitness', {'plans': [plan(enabled=False, price='')]}))
        self.assertIsNone(cleaned['detail']['fitness']['plans'][0]['price'])

    def test_resort_package_rules(self):
        with self.assertRaises(OfferingError):
            clean_record(record('resort', {'packages': [package(minPersons='5', maxPersons='2')]}))
        with self.assertRaises(OfferingError):
            clean_record(record('resort', {'packages': [package(slots=[{'start': '13:00', 'end': '09:00'}])]}))
        with self.assertRaises(OfferingError):     # full day needs check-in/out somewhere
            clean_record(record('resort', {'packages': [package(duration='full_day', slots=[])]}))
        ok = clean_record(record('resort', {'checkIn': '10:00', 'checkOut': '18:00',
                                            'packages': [package(duration='full_day', slots=[])]}))
        self.assertEqual(ok['detail']['resort']['packages'][0]['checkIn'], '10:00')
        with self.assertRaises(OfferingError):     # pools are per person / per hour
            clean_record(record('resort', {'packages': [package(kind='pool', pricing='per_package')]}))

    def test_one_screen_cannot_play_two_shows_at_once(self):
        clash = {**SURPRISE, 'shows': [show(), show(id='show-2', movie='Devara', times=['18:30'])]}
        with self.assertRaises(OfferingError):
            clean_record(record('surprise', clash))
        apart = {**SURPRISE, 'shows': [show(), show(id='show-2', movie='Devara', screen='2', times=['18:30'])]}
        clean_record(record('surprise', apart))
        with self.assertRaises(OfferingError):     # screen 3 of a 2-screen service
            clean_record(record('surprise', {**SURPRISE, 'shows': [show(screen='3')]}))

    def test_venues_without_a_block_are_untouched(self):
        legacy = {'name': 'Old resort', 'category': 'Resort', 'detail': {'primaryCategory': 'Resort', 'unitPrices': ['500']}}
        self.assertEqual(clean_record(legacy), legacy)
        hall = {'name': 'Hall', 'category': 'Party hall', 'detail': {'primaryCategory': 'Private Hall'}}
        self.assertEqual(clean_record(hall), hall)

    def test_membership_end_dates(self):
        start = datetime.date(2026, 1, 31)
        self.assertEqual(add_duration(start, 1, 'day'), start)
        self.assertEqual(add_duration(start, 1, 'week'), datetime.date(2026, 2, 6))
        self.assertEqual(add_duration(start, 1, 'month'), datetime.date(2026, 2, 27))
        self.assertEqual(add_duration(datetime.date(2026, 1, 15), 12, 'month'), datetime.date(2027, 1, 14))


class CompletionTests(SimpleTestCase):
    def _missing(self, details, payout=None):
        draft = VenueDraft(data={
            'basics': {'venueName': 'X', 'phone': '9000000000'},
            'location': {'houseStreet': 'a', 'pincode': '506001', 'stateName': 'TS', 'district': 'Hanamkonda',
                         'city': 'Hanamkonda', 'mapsLink': 'https://maps.app.goo.gl/x'},
            'details': details,
            'payout': payout or {'accountHolder': 'A', 'bankName': 'B', 'accountNumber': '123456789',
                                 'ifsc': 'HDFC0001234', 'phone': '9000000000', 'agreedToTerms': True},
            'photos': {'venuePhotos': [{'url': 'https://x/y.jpg'}]},
        })
        return compute_completion(draft)[1]

    def test_a_gym_needs_a_priced_plan(self):
        self.assertIn('At least one membership plan with a price', self._missing({'primaryCategory': 'Gym'}))
        self.assertEqual(self._missing({'primaryCategory': 'Gym', 'fitness': {'plans': [plan()]}}), [])

    def test_resort_needs_a_package_or_its_legacy_pricing(self):
        self.assertTrue(self._missing({'primaryCategory': 'Resort'}))
        self.assertEqual(self._missing({'primaryCategory': 'Resort', 'resort': {'packages': [package()]}}), [])
        self.assertEqual(self._missing({'primaryCategory': 'Resort', 'capacity': '50',
                                        'packages': [{'label': 'Day', 'price': '500'}]}), [])

    def test_surprise_needs_a_price_and_a_show(self):
        missing = self._missing({'primaryCategory': 'Big Screen Surprise', 'surprise': {}})
        self.assertIn('Price per surprise', missing)
        self.assertEqual(self._missing({'primaryCategory': 'Big Screen Surprise', 'surprise': SURPRISE}), [])

    def test_the_terms_agreement_is_required_for_the_new_forms_only(self):
        payout = {'accountHolder': 'A', 'bankName': 'B', 'accountNumber': '123456789',
                  'ifsc': 'HDFC0001234', 'phone': '9000000000'}
        self.assertIn('Agreement to the platform commission and terms',
                      self._missing({'primaryCategory': 'Gym', 'fitness': {'plans': [plan()]}}, payout))
        hall = {'primaryCategory': 'Private Hall', 'capacity': '50', 'amenities': ['AC'], 'packages': [{'label': 'x'}]}
        self.assertEqual(self._missing(hall, payout), [])


@override_settings(RAZORPAY_KEY_ID='rzp_test_key', RAZORPAY_KEY_SECRET='test_key_secret',
                   SUPABASE_URL='https://store.example.co', SUPABASE_STORAGE_BUCKET='venue-photos')
class OfferingsApiTests(APITestCase):
    _seq = 0

    def setUp(self):
        cache.clear()
        self.vendor = User.objects.create_user(phone='9400000001', name='Owner', role=User.Role.VENDOR)
        self.customer = User.objects.create_user(phone='9400000002', name='Asha')
        self.other = User.objects.create_user(phone='9400000003', name='Ravi')

    def tearDown(self):
        cache.clear()

    # --- helpers --------------------------------------------------------------

    def publish(self, kind, block, listing_id=None, status_after='live', **detail):
        self.client.force_authenticate(self.vendor)
        if listing_id is None:
            listing_id = VenueDraft.objects.create(vendor=self.vendor).pk
        body = {'id': str(listing_id), **record(kind, block, **detail)}
        response = self.client.post('/api/vendors/me/listings', body, format='json')
        if response.status_code == 201 and status_after:
            Listing.objects.filter(pk=listing_id).update(status=status_after)
        self.client.force_authenticate(None)
        return response, listing_id

    def order(self, user, body):
        OfferingsApiTests._seq += 1
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': f'order_OFF{OfferingsApiTests._seq}'}
        self.client.force_authenticate(user)
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            response = self.client.post('/api/offerings/order', body, format='json')
        self.client.force_authenticate(None)
        return response

    def verify(self, user, order):
        payment_id = f'pay_{order.data["orderId"]}'
        signature = hmac.new(b'test_key_secret', f'{order.data["orderId"]}|{payment_id}'.encode(),
                             hashlib.sha256).hexdigest()
        self.client.force_authenticate(user)
        response = self.client.post('/api/payments/verify', {
            'bookingId': order.data['bookingId'], 'razorpay_order_id': order.data['orderId'],
            'razorpay_payment_id': payment_id, 'razorpay_signature': signature,
        }, format='json')
        self.client.force_authenticate(None)
        return response

    def quote(self, user, body):
        self.client.force_authenticate(user)
        response = self.client.post('/api/offerings/quote', body, format='json')
        self.client.force_authenticate(None)
        return response

    # --- publish + sync ---------------------------------------------------------

    def test_publish_syncs_plans_and_archives_removed_ones(self):
        response, lid = self.publish('fitness', {'plans': [plan(), plan(id='p-day', kind='day_pass', name='Day Pass', price='200', durationUnit='day')]})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(FitnessPlan.objects.filter(listing_id=lid).count(), 2)
        self.assertEqual(OfferingProfile.objects.get(listing_id=lid).kind, 'fitness')
        # Republish without the day pass: archived, not deleted.
        self.publish('fitness', {'plans': [plan(price='1800')]}, listing_id=lid)
        day = FitnessPlan.objects.get(listing_id=lid, key='p-day')
        self.assertIsNotNone(day.archived_at)
        self.assertFalse(day.on_sale)
        self.assertEqual(FitnessPlan.objects.get(listing_id=lid, key='p-month').price, 1800)
        # And restored when it comes back.
        self.publish('fitness', {'plans': [plan(), plan(id='p-day', kind='day_pass', name='Day Pass', price='200', durationUnit='day')]}, listing_id=lid)
        self.assertIsNone(FitnessPlan.objects.get(listing_id=lid, key='p-day').archived_at)

    def test_an_invalid_block_is_refused_and_nothing_is_saved(self):
        response, lid = self.publish('fitness', {'plans': [plan(price='-1')]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Listing.objects.filter(pk=lid).exists())
        self.assertFalse(FitnessPlan.objects.exists())

    def test_public_offerings_payload(self):
        _, lid = self.publish('resort', {'packages': [package()]})
        data = self.client.get(f'/api/venues/{lid}/offerings?date={TOMORROW.isoformat()}').data
        self.assertEqual(data['kind'], 'resort')
        slot = data['packages'][0]['availability'][0]
        self.assertEqual((slot['start'], slot['available'], slot['remaining']), ('09:00', True, 12))
        self.assertIn('Welcome drink', data['packages'][0]['inclusions'])
        # Pending venues are not public.
        Listing.objects.filter(pk=lid).update(status='pending')
        self.assertEqual(self.client.get(f'/api/venues/{lid}/offerings').status_code, 404)

    def test_options_endpoint(self):
        data = self.client.get('/api/offerings/options').data
        self.assertEqual(len(data['fitnessServices']), 12)
        self.assertIn('Selfie on Screen', data['surpriseOccasions'])

    # --- memberships ------------------------------------------------------------

    def test_membership_purchase_flow(self):
        _, lid = self.publish('fitness', {'plans': [plan()]})
        body = {'kind': 'fitness', 'venueId': str(lid), 'planId': 'p-month'}
        quote = self.quote(self.customer, body)
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data['amount'], 1500 + 500 + 20)      # plan + joining fee + platform fee
        self.assertEqual(quote.data['endsOn'], add_duration(today_ist(), 1, 'month').isoformat())

        self.assertEqual(self.order(self.customer, {**body, 'amount': 999}).status_code, 400)   # tampered
        order = self.order(self.customer, {**body, 'amount': quote.data['amount']})
        self.assertEqual(order.status_code, 201, order.data)
        booking = Booking.objects.get(pk=order.data['bookingId'])
        self.assertEqual((booking.kind, booking.status, booking.slots), ('membership', 'payment_pending', []))
        self.assertTrue(booking.invoice_number)

        verified = self.verify(self.customer, order)
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertEqual(verified.data['booking']['offering']['state'], 'active')

        self.client.force_authenticate(self.customer)
        mine = self.client.get('/api/users/me/memberships').data['memberships']
        self.assertEqual([m['id'] for m in mine], [booking.id])
        # Memberships are not mixed into the date-based booking list…
        self.assertEqual(self.client.get('/api/users/me/bookings').data['bookings'], [])
        # …and a paid one cannot be cancelled from the app.
        self.assertEqual(self.client.delete(f'/api/users/me/bookings/{booking.id}').status_code, 400)
        # The joining fee is not charged a second time.
        self.assertEqual(self.quote(self.customer, body).data['amount'], 1500 + 20)

    def test_chosen_start_dates_are_bounded(self):
        _, lid = self.publish('fitness', {'plans': [plan(startRule='chosen', startWithinDays='10')]})
        body = {'kind': 'fitness', 'venueId': str(lid), 'planId': 'p-month'}
        ok = self.quote(self.customer, {**body, 'startDate': IN_A_WEEK.isoformat()})
        self.assertEqual(ok.data['startsOn'], IN_A_WEEK.isoformat())
        far = (today_ist() + datetime.timedelta(days=30)).isoformat()
        self.assertEqual(self.quote(self.customer, {**body, 'startDate': far}).status_code, 400)

    def test_vendor_records_visits(self):
        _, lid = self.publish('fitness', {'plans': [plan(visits='2', expiryRule='visits_or_duration')]})
        body = {'kind': 'fitness', 'venueId': str(lid), 'planId': 'p-month'}
        order = self.order(self.customer, body)
        self.verify(self.customer, order)
        self.client.force_authenticate(self.vendor)
        url = f'/api/vendors/me/memberships/{order.data["bookingId"]}/visits'
        first = self.client.post(url)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data['membership']['visitsRemaining'], 1)
        self.assertEqual(self.client.post(url).status_code, 409)       # once a day
        members = self.client.get('/api/vendors/me/memberships').data['memberships']
        self.assertEqual(members[0]['customer'], 'Asha')
        # Another vendor can't touch it.
        stranger = User.objects.create_user(phone='9400000009', name='X', role=User.Role.VENDOR)
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.post(url).status_code, 404)

    def test_unpaid_memberships_are_not_valid_and_checkin_refuses_memberships(self):
        _, lid = self.publish('fitness', {'plans': [plan()]})
        order = self.order(self.customer, {'kind': 'fitness', 'venueId': str(lid), 'planId': 'p-month'})
        self.client.force_authenticate(self.vendor)
        url = f'/api/vendors/me/memberships/{order.data["bookingId"]}/visits'
        refused = self.client.post(url)                                 # unpaid → no visits
        self.assertEqual(refused.status_code, 409)
        self.assertIn('not active (pending)', refused.data['message'])
        self.verify(self.customer, order)
        from bookings.checkin import INELIGIBLE, evaluate
        booking = Booking.objects.get(pk=order.data['bookingId'])
        self.assertEqual(evaluate('venue', booking)[0], INELIGIBLE)

    # --- resort packages ------------------------------------------------------

    def test_package_pricing_and_capacity(self):
        _, lid = self.publish('resort', {'packages': [package()]})
        body = {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pkg-family',
                'date': TOMORROW.isoformat(), 'slot': '09:00-13:00', 'adults': 4, 'children': 2}
        quote = self.quote(self.customer, body)
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data['amount'], 4 * 800 + 2 * 400 + 20)
        self.assertEqual(self.quote(self.customer, {**body, 'adults': 1, 'children': 0}).status_code, 400)   # min 2
        self.assertEqual(self.quote(self.customer, {**body, 'adults': 11}).status_code, 400)                # max 10

        first = self.order(self.customer, body)                        # 6 of 12 places
        self.assertEqual(first.status_code, 201, first.data)
        second = self.order(self.other, {**body, 'adults': 6, 'children': 1})   # 7 more → over 12
        self.assertEqual(second.status_code, 409)
        third = self.order(self.other, {**body, 'adults': 6, 'children': 0})    # exactly 12
        self.assertEqual(third.status_code, 201, third.data)
        data = self.client.get(f'/api/venues/{lid}/offerings?date={TOMORROW.isoformat()}').data
        self.assertFalse(data['packages'][0]['availability'][0]['available'])

    def test_package_days_closures_and_windows(self):
        weekday = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[TOMORROW.weekday()]
        others = [d for d in ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun') if d != weekday]
        _, lid = self.publish('resort', {'availableDays': others, 'packages': [package()]})
        body = {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pkg-family',
                'date': TOMORROW.isoformat(), 'slot': '09:00-13:00', 'adults': 2}
        self.assertEqual(self.quote(self.customer, body).status_code, 409)
        self.publish('resort', {'closures': [{'from': TOMORROW.isoformat(), 'to': TOMORROW.isoformat(), 'reason': 'Maintenance'}],
                                'packages': [package()]}, listing_id=lid)
        self.assertIn('Maintenance', self.quote(self.customer, body).data['message'])
        self.publish('resort', {'packages': [package(availableFrom=IN_A_WEEK.isoformat())]}, listing_id=lid)
        self.assertEqual(self.quote(self.customer, body).status_code, 409)

    def test_pool_per_hour_adult_and_child_prices(self):
        pool = package(id='pool', name='Pool', kind='pool', duration='', pricing='per_hour', price='150',
                       childPrice='80', minPersons='1', maxPersons='20', capacity='',
                       slots=[{'start': '10:00', 'end': '12:00'}])
        _, lid = self.publish('resort', {'packages': [pool]})
        quote = self.quote(self.customer, {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pool',
                                           'date': TOMORROW.isoformat(), 'slot': '10:00', 'adults': 2, 'children': 1})
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data['amount'], (2 * 150 + 80) * 2 + 20)

    def test_package_bookings_never_block_slot_bookings(self):
        _, lid = self.publish('resort', {'packages': [package()]})
        self.order(self.customer, {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pkg-family',
                                   'date': TOMORROW.isoformat(), 'slot': '09:00-13:00', 'adults': 2})
        availability = self.client.get(f'/api/venues/{lid}/availability?date={TOMORROW.isoformat()}').data
        self.assertEqual(availability['booked'], [])

    def test_a_late_payment_after_the_hold_expired_and_the_place_resold_is_refunded(self):
        _, lid = self.publish('resort', {'packages': [package(capacity='6')]})
        body = {'kind': 'resort', 'venueId': str(lid), 'packageId': 'pkg-family',
                'date': TOMORROW.isoformat(), 'slot': '09:00-13:00', 'adults': 6}
        stale = self.order(self.customer, body)
        Booking.objects.filter(pk=stale.data['bookingId']).update(
            created_at=timezone.now() - datetime.timedelta(hours=1),
            hold_expires_at=timezone.now() - datetime.timedelta(minutes=55),
        )
        fresh = self.order(self.other, body)
        self.assertEqual(fresh.status_code, 201, fresh.data)           # the expired hold freed the places
        late = self.verify(self.customer, stale)
        self.assertEqual(late.status_code, 409)
        self.assertEqual(Booking.objects.get(pk=stale.data['bookingId']).status, 'refund_pending')

    # --- big screen surprise --------------------------------------------------

    def surprise_body(self, lid, **over):
        return {'kind': 'surprise', 'venueId': str(lid), 'showId': 'show-1', 'date': TOMORROW.isoformat(),
                'time': '18:30', 'occasion': 'Birthday', 'recipientName': 'Meera',
                'message': 'Happy birthday Meera!', 'senderName': 'Arjun', **over}

    def test_surprise_booking_and_no_double_booking_of_a_screen(self):
        _, lid = self.publish('surprise', SURPRISE, occasions=['Birthday', 'Proposal'])
        quote = self.quote(self.customer, self.surprise_body(lid))
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data['amount'], 999 + 20)
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, occasion='Proposal')).data['amount'], 1499 + 20)
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, occasion='Love')).status_code, 400)
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, time='20:00')).status_code, 400)
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, recipientName='')).status_code, 400)

        first = self.order(self.customer, self.surprise_body(lid))
        self.assertEqual(first.status_code, 201, first.data)
        second = self.order(self.other, self.surprise_body(lid))
        self.assertEqual(second.status_code, 409)                      # same screen + time
        other_time = self.order(self.other, self.surprise_body(lid, time='21:30'))
        self.assertEqual(other_time.status_code, 201)
        self.verify(self.customer, first)
        record_ = Booking.objects.get(pk=first.data['bookingId']).as_record()
        self.assertEqual(record_['kind'], 'surprise')
        self.assertEqual(record_['offering']['recipientName'], 'Meera')
        data = self.client.get(f'/api/venues/{lid}/offerings?date={TOMORROW.isoformat()}').data
        times = {t['time']: t['available'] for t in data['shows'][0]['availability']}
        self.assertEqual(times, {'18:30': False, '21:30': False})

    def test_surprise_media_rules(self):
        _, lid = self.publish('surprise', SURPRISE, occasions=['Birthday'])
        foreign = self.surprise_body(lid, photoUrl='https://evil.example.com/x.jpg')
        self.assertEqual(self.quote(self.customer, foreign).status_code, 400)
        own = f'https://store.example.co/storage/v1/object/public/venue-photos/surprises/{self.customer.id}/a.jpg'
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, photoUrl=own)).status_code, 200)
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid, videoUrl=own.replace('a.jpg', 'v.mp4'))).status_code, 400)  # videos not allowed
        self.client.force_authenticate(self.customer)
        bad = self.client.post('/api/offerings/surprise-media', {'type': 'video', 'venueId': str(lid)})
        self.assertEqual(bad.status_code, 400)

    def test_shows_per_day_cap_and_lead_time(self):
        _, lid = self.publish('surprise', {**SURPRISE, 'showsPerDay': '1'}, occasions=['Birthday'])
        self.assertEqual(self.order(self.customer, self.surprise_body(lid)).status_code, 201)
        self.assertEqual(self.order(self.other, self.surprise_body(lid, time='21:30')).status_code, 409)
        _, lid2 = self.publish('surprise', {**SURPRISE, 'minLeadHours': '72'}, occasions=['Birthday'])
        self.assertEqual(self.quote(self.customer, self.surprise_body(lid2)).status_code, 409)

    # --- super admin edit path ------------------------------------------------

    def test_admin_edit_validates_and_syncs(self):
        _, lid = self.publish('resort', {'packages': [package()]})
        admin = User.objects.create_user(phone='9400000010', name='Admin', role=User.Role.ADMIN) \
            if hasattr(User.Role, 'ADMIN') else User.objects.create_superuser(phone='9400000010', name='Admin')
        from adminpanel.venue_edit import EditError, apply_edit, version_of
        listing = Listing.objects.get(pk=lid)
        sections = {'basics': {'venueName': 'Venue'}, 'details': {
            'primaryCategory': 'Resort', 'openTime': '09:00', 'closeTime': '18:00', 'open24Hours': False,
            'resort': {'packages': [package(price='900')]}}}
        good = record('resort', {'packages': [package(price='900')]})
        apply_edit(listing, {'version': version_of(listing), 'sections': sections, 'record': good})
        self.assertEqual(ResortPackage.objects.get(listing_id=lid).price, 900)
        listing.refresh_from_db()
        with self.assertRaises(EditError):
            apply_edit(listing, {'version': version_of(listing), 'sections': sections,
                                 'record': record('resort', {'packages': [package(maxPersons='1')]})})
        self.assertTrue(admin.pk)
