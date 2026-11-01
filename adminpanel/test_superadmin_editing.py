"""
Super-admin control of existing venues and vendors — regression tests.

Venue location (adminpanel/venue_edit.py):
  * the edit payload and the admin venue rows say whether a venue has a pin
  * a save that adds / moves / clears the pin needs locationConfirmed
  * typed coordinates must be a real pair inside India
  * typed coordinates that disagree with the link's own exact pin are refused
  * a confirmed pin reaches the public distance search (radius + distance)
  * an unrelated edit of a pinned venue needs no confirmation

Photos / history:
  * every save is a VenueEditRevision; removed photos are listed there
  * the audit line carries old → new values

Vendor profile (adminpanel/vendor_profile.py):
  * super admin only; name / email / business details save
  * bank details: never returned, refused anywhere in a request, unchanged
  * sign-in phone, role, KYC, active state cannot be changed here
  * duplicate email and stale version refused; audited

Bookings, payments and ownership survive every edit.
"""
import copy
import datetime
import uuid

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User, VendorSettings
from bookings.models import Booking
from bookings.slots import today_ist
from venues.models import Listing, PayoutDetails, VenueDraft

from .models import AuditEntry, VenueEditRevision
from .test_venue_edit import DRAFT, RECORD

# Hanamkonda, Warangal — a real point in Telangana.
PIN = (17.9689, 79.5941)
# A second real point ~2 km away.
OTHER = (17.9850, 79.6050)
PIN_LINK = f'https://www.google.com/maps/search/?api=1&query={OTHER[0]},{OTHER[1]}'


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user(phone='9990000301', name='Anita', email='an@x.in',
                                              role=User.Role.ADMIN, password='pw')
        self.vendor = User.objects.create_user(phone='9000000011', name='Ravi', email='v@x.in',
                                               role=User.Role.VENDOR)
        self.customer = User.objects.create_user(phone='9100000011', name='Asha', email='c@x.in')
        self.admin_client = APIClient()
        self.admin_client.force_login(self.admin)


class VenueLocationAndHistoryTests(_Base):
    def setUp(self):
        super().setUp()
        self.listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall', record=copy.deepcopy(RECORD),
            name='Grand Hall', category='Party hall', locality='Hanamkonda', pincode='506001', status='live',
        )
        self.draft = VenueDraft.objects.create(id=self.listing.pk, vendor=self.vendor,
                                               data=copy.deepcopy(DRAFT), status='pending')
        self.url = f'/api/admin/venues/{self.listing.pk}/edit'

    def load(self):
        response = self.admin_client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def body(self, location=None, photos=None, record_detail=None, confirmed=None, record=None):
        """A PUT body built the way the panel builds it: the edited
        sections plus the record the mapper rebuilds from them."""
        loaded = self.load()
        loc = {**DRAFT['location'], **(location or {})}
        detail = {**RECORD['detail'], 'mapsLink': loc.get('mapsLink', '')}
        try:
            detail['mapPin'] = {'lat': float(loc['latitude']), 'lng': float(loc['longitude'])}
        except (KeyError, TypeError, ValueError):
            pass                     # the mapper leaves mapPin out when empty / junk
        detail.update(record_detail or {})
        sections = {'basics': DRAFT['basics'], 'location': loc, 'details': DRAFT['details']}
        if photos is not None:
            sections['photos'] = photos
        out = {
            'version': loaded['version'],
            'sections': sections,
            'record': record or {**RECORD, 'detail': detail},
        }
        if confirmed is not None:
            out['locationConfirmed'] = confirmed
        return out

    def put(self, body):
        return self.admin_client.put(self.url, body, format='json')

    def pin_of(self):
        listing = Listing.objects.get(pk=self.listing.pk)
        return listing.latitude, listing.longitude

    # -- seeing which venues have no pin ----------------------------------

    def test_missing_pin_is_reported(self):
        data = self.load()
        self.assertEqual(data['location']['pinStatus'], 'missing')
        self.assertIsNone(data['location']['coordinates'])
        self.assertEqual(data['venue']['pinStatus'], 'missing')
        self.assertEqual(data['revisions'], [])

    # -- confirming a location ------------------------------------------

    def test_adding_a_pin_needs_confirmation(self):
        body = self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])})
        response = self.put(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'LOCATION_UNCONFIRMED')
        self.assertEqual(self.pin_of(), (None, None))
        self.assertFalse(VenueEditRevision.objects.exists())

    def test_confirmed_pin_is_saved_and_used_by_the_distance_search(self):
        body = self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True)
        response = self.put(body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.pin_of(), PIN)
        data = response.json()
        self.assertEqual(data['location']['pinStatus'], 'ok')
        self.assertEqual(data['location']['pinSource'], 'pin')
        # The vendor's draft carries it, so the vendor portal's republish keeps it.
        location = VenueDraft.objects.get(pk=self.draft.pk).data['location']
        self.assertEqual((location['latitude'], location['longitude']), (str(PIN[0]), str(PIN[1])))
        # Customer site: inside a 30 km radius around a nearby point, with a distance.
        cache.clear()
        found = self.client.get('/api/venues', {'lat': '17.97', 'lng': '79.60', 'radius': '30'}).json()
        rows = found if isinstance(found, list) else found.get('results') or found.get('venues') or []
        row = next(r for r in rows if r.get('id') == str(self.listing.pk))
        self.assertEqual(row['coordinates'], {'lat': PIN[0], 'lng': PIN[1]})
        self.assertIsNotNone(row.get('distanceKm', row.get('distance')))
        detail = self.client.get(f'/api/venues/{self.listing.pk}').json()
        self.assertEqual(detail['coordinates'], {'lat': PIN[0], 'lng': PIN[1]})

    def test_moving_and_clearing_a_pin_need_confirmation(self):
        self.put(self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True))
        moved = self.body(location={'latitude': str(OTHER[0]), 'longitude': str(OTHER[1])})
        self.assertEqual(self.put(moved).json()['code'], 'LOCATION_UNCONFIRMED')
        self.assertEqual(self.pin_of(), PIN)
        moved['locationConfirmed'] = True
        self.assertEqual(self.put(moved).status_code, 200)
        self.assertEqual(self.pin_of(), OTHER)
        cleared = self.body(location={'latitude': '', 'longitude': ''})
        self.assertEqual(self.put(cleared).json()['code'], 'LOCATION_UNCONFIRMED')
        cleared['locationConfirmed'] = True
        self.assertEqual(self.put(cleared).status_code, 200)
        self.assertEqual(self.pin_of(), (None, None))
        self.assertNotIn('mapPin', Listing.objects.get(pk=self.listing.pk).record['detail'])

    def test_pitches_and_screens_follow_the_pin_both_ways(self):
        sibling = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall-2', name='Grand Hall — Screen 2',
            category='Party hall', status='live',
            record={**copy.deepcopy(RECORD), 'name': 'Grand Hall — Screen 2',
                    'detail': {**RECORD['detail'], 'unitOf': str(self.listing.pk), 'unitLabel': 'Screen 2',
                               'mapPin': {'lat': OTHER[0], 'lng': OTHER[1]}}},
        )
        self.put(self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True))
        sibling.refresh_from_db()
        self.assertEqual((sibling.latitude, sibling.longitude), PIN)
        self.put(self.body(location={'latitude': '', 'longitude': ''}, confirmed=True))
        sibling.refresh_from_db()
        self.assertEqual((sibling.latitude, sibling.longitude), (None, None))
        self.assertNotIn('mapPin', sibling.record['detail'])
        self.assertEqual(sibling.record['detail']['unitOf'], str(self.listing.pk))   # still a unit

    def test_unrelated_edit_of_a_pinned_venue_needs_no_confirmation(self):
        self.put(self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True))
        body = self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])},
                         record_detail={'description': 'Fresh words'})
        response = self.put(body)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.pin_of(), PIN)

    def test_coordinates_must_be_a_real_pair_inside_india(self):
        cases = [
            ({'latitude': str(PIN[0]), 'longitude': ''}, 'PIN_INVALID'),
            ({'latitude': 'abc', 'longitude': '79.5'}, 'PIN_INVALID'),
            ({'latitude': '95', 'longitude': '79.5'}, 'PIN_INVALID'),
            ({'latitude': str(PIN[1]), 'longitude': str(PIN[0])}, 'PIN_OUTSIDE_INDIA'),   # swapped
            ({'latitude': '51.5', 'longitude': '-0.12'}, 'PIN_OUTSIDE_INDIA'),           # London
        ]
        for location, code in cases:
            response = self.put(self.body(location=location, confirmed=True,
                                          record={**RECORD, 'detail': dict(RECORD['detail'])}))
            self.assertEqual(response.status_code, 400, location)
            self.assertEqual(response.json()['code'], code, location)
        self.assertEqual(self.pin_of(), (None, None))

    def test_typed_pin_that_disagrees_with_the_links_pin_is_refused(self):
        body = self.body(location={'mapsLink': PIN_LINK, 'latitude': str(PIN[0]), 'longitude': str(PIN[1])},
                         confirmed=True)
        response = self.put(body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'PIN_CONFLICT')

    def test_replacing_a_wrong_link_corrects_the_pin(self):
        wrong = f'https://www.google.com/maps/search/?api=1&query={OTHER[0]},{OTHER[1]}'
        self.put(self.body(location={'mapsLink': wrong}, confirmed=True))
        self.assertEqual(self.pin_of(), OTHER)
        right = f'https://www.google.com/maps/search/?api=1&query={PIN[0]},{PIN[1]}'
        response = self.put(self.body(location={'mapsLink': right}, confirmed=True))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.pin_of(), PIN)
        self.assertEqual(response.json()['location']['pinSource'], 'link')

    # -- history, photos, audit ---------------------------------------------

    def test_every_save_is_a_revision_and_removed_photos_are_kept(self):
        photos = {'venuePhotos': [{'id': 'p2', 'url': 'https://cdn.example/2.jpg'}], 'serviceImages': []}
        record = {**RECORD, 'image': 'https://cdn.example/2.jpg', 'gallery': ['https://cdn.example/2.jpg']}
        response = self.put(self.body(photos=photos, record=record))
        self.assertEqual(response.status_code, 200, response.content)
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual(listing.record['image'], 'https://cdn.example/2.jpg')
        revision = VenueEditRevision.objects.get(listing_id=str(self.listing.pk))
        self.assertEqual(revision.admin, 'Anita')
        self.assertEqual(revision.removed_photos, [{'gallery': 'venuePhotos', 'url': 'https://cdn.example/1.jpg'}])
        self.assertEqual(revision.before_record['image'], 'https://cdn.example/1.jpg')
        self.assertNotIn('payout', revision.before_sections)
        self.assertNotIn('123456789012', str(revision.before_sections))
        shown = response.json()['revisions']
        self.assertEqual(shown[0]['removedPhotos'][0]['url'], 'https://cdn.example/1.jpg')
        audit = AuditEntry.objects.filter(action='Edited venue details').first()
        self.assertIn('1 photo(s) removed', audit.change)

    def test_photo_order_decides_the_cover(self):
        photos = {'venuePhotos': [{'id': 'p2', 'url': 'https://cdn.example/2.jpg'},
                                  {'id': 'p1', 'url': 'https://cdn.example/1.jpg'}], 'serviceImages': []}
        record = {**RECORD, 'image': 'https://cdn.example/2.jpg',
                  'gallery': ['https://cdn.example/2.jpg', 'https://cdn.example/1.jpg']}
        self.assertEqual(self.put(self.body(photos=photos, record=record)).status_code, 200)
        cache.clear()
        public = self.client.get(f'/api/venues/{self.listing.pk}').json()
        self.assertEqual(public['image'], 'https://cdn.example/2.jpg')
        self.assertEqual(public['gallery'][0], 'https://cdn.example/2.jpg')
        draft_photos = VenueDraft.objects.get(pk=self.draft.pk).data['photos']['venuePhotos']
        self.assertEqual([p['id'] for p in draft_photos], ['p2', 'p1'])
        self.assertFalse(VenueEditRevision.objects.get().removed_photos)

    def test_audit_has_old_and_new_values(self):
        record = {**RECORD, 'name': 'Grand Palace', 'detail': {**RECORD['detail'], 'mapPin': {'lat': PIN[0], 'lng': PIN[1]}}}
        body = self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True, record=record)
        body['sections']['basics'] = {**DRAFT['basics'], 'venueName': 'Grand Palace'}
        self.assertEqual(self.put(body).status_code, 200)
        change = AuditEntry.objects.filter(action='Edited venue details').first().change
        self.assertIn('name: Grand Hall → Grand Palace', change)
        self.assertIn(f'pin: none → {PIN[0]:.6f},{PIN[1]:.6f}', change)
        self.assertLessEqual(len(change), 300)

    # -- bookings, payments, ownership -----------------------------------------

    def test_bookings_payments_and_owner_survive_an_edit(self):
        booking = Booking.objects.create(
            listing=self.listing, user=self.customer, date=today_ist() + datetime.timedelta(days=3),
            slots=['19:00 – 20:00'], amount=1520, fee=20, customer_name='Asha', venue_name='Grand Hall',
            phone='9100000011', status='confirmed', razorpay_payment_id='pay_123', method='online',
            razorpay_order_id='order_123',
        )
        before = {f.name: getattr(booking, f.name) for f in Booking._meta.fields if f.name != 'updated_at'}
        record = {**RECORD, 'name': 'Grand Palace', 'detail': {**RECORD['detail'], 'mapPin': {'lat': PIN[0], 'lng': PIN[1]}}}
        body = self.body(location={'latitude': str(PIN[0]), 'longitude': str(PIN[1])}, confirmed=True, record=record)
        self.assertEqual(self.put(body).status_code, 200)
        booking.refresh_from_db()
        after = {f.name: getattr(booking, f.name) for f in Booking._meta.fields if f.name != 'updated_at'}
        self.assertEqual(before, after)
        listing = Listing.objects.get(pk=self.listing.pk)
        self.assertEqual(listing.vendor_id, self.vendor.pk)
        self.assertEqual(listing.slug, 'grand-hall')              # the venue URL keeps working
        self.assertEqual(listing.status, 'live')
        self.assertEqual(VenueDraft.objects.get(pk=self.draft.pk).vendor_id, self.vendor.pk)


class VendorProfileTests(_Base):
    def setUp(self):
        super().setUp()
        self.payout = PayoutDetails.objects.create(
            user=self.vendor, account_holder='Ravi', bank_name='HDFC', account_number='123456789012',
            ifsc='HDFC0001234', payout_phone='9000000011',
        )
        VendorSettings.objects.create(user=self.vendor, business_name='Ravi Events', business_address='1 Main Rd')
        self.url = f'/api/admin/vendors/{self.vendor.pk}/profile'

    def load(self, client=None):
        response = (client or self.admin_client).get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def put(self, body, client=None):
        return (client or self.admin_client).put(self.url, body, format='json')

    def edit(self, **parts):
        return {'version': self.load()['version'], **parts}

    def assert_bank_untouched(self):
        payout = PayoutDetails.objects.get(pk=self.payout.pk)
        self.assertEqual((payout.account_number, payout.ifsc, payout.bank_name, payout.account_holder),
                         ('123456789012', 'HDFC0001234', 'HDFC', 'Ravi'))

    def test_only_a_super_admin_may_view_or_edit(self):
        body = self.edit(profile={'name': 'Hacker'})
        for user in (None, self.vendor, self.customer):
            client = APIClient()
            if user is not None:
                client.force_login(user)
            self.assertIn(client.get(self.url).status_code, (401, 403))
            self.assertIn(self.put(body, client).status_code, (401, 403))
        self.assertEqual(User.objects.get(pk=self.vendor.pk).name, 'Ravi')

    def test_get_never_returns_bank_details(self):
        data = self.load()
        text = str(data)
        for secret in ('123456789012', 'HDFC0001234', 'HDFC'):
            self.assertNotIn(secret, text)
        self.assertFalse(data['bankDetailsEditable'])
        self.assertEqual(data['business']['name'], 'Ravi Events')

    def test_profile_and_business_details_save_and_are_audited(self):
        body = self.edit(
            profile={'name': 'Ravi Kumar', 'email': 'ravi@x.in'},
            business={'name': 'RK Events', 'phone': '9876543210', 'email': 'desk@rk.in',
                      'address': '2 Station Rd', 'city': 'Warangal', 'state': 'Telangana'},
        )
        response = self.put(body)
        self.assertEqual(response.status_code, 200, response.content)
        vendor = User.objects.get(pk=self.vendor.pk)
        self.assertEqual((vendor.name, vendor.email), ('Ravi Kumar', 'ravi@x.in'))
        row = VendorSettings.objects.get(user=vendor)
        self.assertEqual((row.business_name, row.business_phone, row.business_city, row.business_state),
                         ('RK Events', '9876543210', 'Warangal', 'Telangana'))
        # Identity and admin-owned state unchanged.
        self.assertEqual((vendor.phone, vendor.role, vendor.kyc, vendor.is_active),
                         ('9000000011', User.Role.VENDOR, 'pending', True))
        self.assert_bank_untouched()
        audit = AuditEntry.objects.get(action='Edited vendor details')
        self.assertIn('contact name: Ravi → Ravi Kumar', audit.change)
        self.assertIn('city: — → Warangal', audit.change)
        self.assertNotIn('123456789012', audit.change)
        # The vendor sees it in their own settings.
        vendor_client = APIClient()
        vendor_client.force_authenticate(vendor)
        mine = vendor_client.get('/api/vendors/me/settings').json()
        self.assertEqual(mine['business']['city'], 'Warangal')

    def test_bank_details_are_refused_anywhere(self):
        attempts = [
            {'business': {'ifsc': 'SBIN0000001'}},
            {'profile': {'name': 'Ravi', 'accountNumber': '999999999999'}},
            {'payout': {'acctNumber': '999999999999'}},
            {'business': {'name': 'X', 'extra': {'bankName': 'SBI'}}},
        ]
        for parts in attempts:
            response = self.put(self.edit(**parts))
            self.assertEqual(response.status_code, 400, parts)
            self.assertEqual(response.json()['code'], 'BANK_DETAILS_READ_ONLY', parts)
        self.assert_bank_untouched()
        self.assertEqual(User.objects.get(pk=self.vendor.pk).name, 'Ravi')

    def test_sign_in_phone_role_and_kyc_cannot_be_changed_here(self):
        response = self.put(self.edit(profile={'phone': '9123456789'}))
        self.assertEqual(response.json()['code'], 'SIGN_IN_PHONE_READ_ONLY')
        for parts in ({'profile': {'role': 'ADMIN'}}, {'profile': {'kyc': 'verified'}}, {'is_active': False}):
            self.assertEqual(self.put(self.edit(**parts)).status_code, 400, parts)
        vendor = User.objects.get(pk=self.vendor.pk)
        self.assertEqual((vendor.phone, vendor.role, vendor.kyc), ('9000000011', User.Role.VENDOR, 'pending'))

    def test_validation(self):
        cases = [
            {'profile': {'name': '  '}},
            {'profile': {'email': 'not-an-email'}},
            {'business': {'phone': '12345'}},
            {'business': {'email': 'nope'}},
            {'business': {'city': 'x' * 101}},
        ]
        for parts in cases:
            self.assertEqual(self.put(self.edit(**parts)).status_code, 400, parts)

    def test_duplicate_email_and_stale_version_are_refused(self):
        response = self.put(self.edit(profile={'email': 'C@X.IN'}))      # the customer's, other case
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'EMAIL_TAKEN')
        stale = self.edit(profile={'name': 'First'})
        self.assertEqual(self.put(self.edit(profile={'name': 'Second'})).status_code, 200)
        response = self.put(stale)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'STALE_VERSION')
        self.assertEqual(User.objects.get(pk=self.vendor.pk).name, 'Second')

    def test_admin_accounts_are_not_edited_from_the_vendors_page(self):
        self.admin.is_vendor = True
        self.admin.save()
        response = self.admin_client.get(f'/api/admin/vendors/{self.admin.pk}/profile')
        self.assertEqual(response.status_code, 403)

    def test_venues_and_bookings_are_untouched_by_a_vendor_edit(self):
        listing = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-hall', record=copy.deepcopy(RECORD),
            name='Grand Hall', category='Party hall', status='live',
        )
        booking = Booking.objects.create(
            listing=listing, user=self.customer, date=today_ist() + datetime.timedelta(days=2),
            slots=['19:00 – 20:00'], amount=1520, fee=20, customer_name='Asha', venue_name='Grand Hall',
            phone='9100000011', status='confirmed', razorpay_payment_id='pay_9', method='online',
            razorpay_order_id='order_9',
        )
        self.assertEqual(self.put(self.edit(profile={'name': 'Ravi Kumar'})).status_code, 200)
        listing.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual((listing.vendor_id, listing.status, listing.record), (self.vendor.pk, 'live', RECORD))
        self.assertEqual((booking.status, booking.amount, booking.razorpay_payment_id), ('confirmed', 1520, 'pay_9'))
