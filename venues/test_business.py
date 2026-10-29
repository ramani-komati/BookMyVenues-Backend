"""
Vendor business controls (venues/business.py) — price rules, opening hours,
closures, blocked slots and the vendor API around them.

The rules under test:
  * a venue with no settings books and prices exactly as before
  * weekday / weekend / time-band / date rules price each 30-minute step,
    with the documented precedence; same-precedence overlaps are refused
  * the server recomputes every amount — a stale client price is refused
  * existing bookings (confirmed or a pending payment) never change amount
  * bookings outside the hours, on a closed day or on a block are refused,
    by the API and again under the lock when the payment order is created
  * a schedule change that strands a booking needs the vendor to acknowledge
    it (the booking stays confirmed); a block over a booking is refused
  * only the owning vendor can read or change any of it
"""
import datetime
import uuid

from accounts.models import User
from bookings.models import Booking
from bookings.slots import today_ist
from bookings.tests import BookingTestBase

from .models import Listing, SlotBlock, VenueBusinessSettings

ALL_DAY = [{'start': '06:00', 'end': '24:00'}]


def next_weekday(weekday, after=None):
    """The next date (after `after`, default today) falling on `weekday`."""
    start = after or today_ist()
    days = (weekday - start.weekday()) % 7 or 7
    return start + datetime.timedelta(days=days)


def week(**overrides):
    """Weekly hours, every day 06:00–24:00 unless overridden by index."""
    hours = {str(i): ALL_DAY for i in range(7)}
    hours.update({str(k): v for k, v in overrides.items()})
    return hours


class BusinessTestBase(BookingTestBase):
    def setUp(self):
        super().setUp()
        self.monday = next_weekday(0)
        self.tuesday = self.monday + datetime.timedelta(days=1)
        self.saturday = next_weekday(5)

    # --- vendor API ---------------------------------------------------
    def as_vendor(self, user=None):
        self.client.force_authenticate(user=user or self.vendor)

    def as_customer(self):
        self.client.force_authenticate(user=self.customer)

    def url(self, suffix='business', listing=None):
        return f'/api/vendors/me/venues/{(listing or self.listing).id}/{suffix}'

    def revision(self, listing=None):
        row = VenueBusinessSettings.objects.filter(listing=listing or self.listing).first()
        return row.revision if row else 0

    def put_rules(self, rules, listing=None):
        self.as_vendor()
        response = self.client.put(
            self.url('business/pricing', listing),
            {'revision': self.revision(listing), 'priceRules': rules}, format='json',
        )
        self.as_customer()
        return response

    def put_schedule(self, ack=None, listing=None, **fields):
        self.as_vendor()
        body = {'revision': self.revision(listing), **fields}
        if ack is not None:
            body['acknowledgeConflicts'] = ack
        response = self.client.put(self.url('business/schedule', listing), body, format='json')
        self.as_customer()
        return response

    def block(self, date, start, end, **extra):
        self.as_vendor()
        response = self.client.post(
            self.url('blocks'),
            {'date': date.isoformat(), 'start': start, 'end': end, **extra}, format='json',
        )
        self.as_customer()
        return response

    # --- customer side --------------------------------------------------
    def book_on(self, date, slots, amount, listing=None, **extra):
        return self.gateway_book({
            'venueId': str((listing or self.listing).id), 'date': date.isoformat(),
            'slots': slots, 'addons': [], 'perSlot': 600, 'amount': amount, **extra,
        })

    def availability(self, date, listing=None):
        self.client.force_authenticate(user=None)
        data = self.client.get(
            f'/api/venues/{(listing or self.listing).id}/availability?date={date.isoformat()}'
        ).data
        self.as_customer()
        return data


class DefaultBehaviourTests(BusinessTestBase):
    def test_a_venue_without_settings_prices_and_books_as_before(self):
        # 1.5h x 600 + ₹20 — the long-standing formula.
        response = self.book_on(self.monday, ['19:30 – 21:00'], 920)
        self.assertEqual(response.status_code, 201)
        data = self.availability(self.tuesday)
        self.assertEqual(data['open'], [{'start': '06:00', 'end': '24:00'}])
        self.assertFalse(data['closed'])
        self.assertEqual(data['unavailable'], [])
        self.assertEqual(
            data['priceBands'],
            [{'start': '06:00', 'end': '24:00', 'rate': None, 'label': None, 'ruleId': None}],
        )

    def test_early_morning_still_bookable_by_default(self):
        self.assertEqual(self.book_on(self.monday, ['06:00 – 07:00'], 620).status_code, 201)


class PriceRuleTests(BusinessTestBase):
    WEEKDAY_EVENING = {
        'label': 'Weekday evening', 'kind': 'weekly', 'days': [0, 1, 2, 3, 4],
        'start': '17:00', 'end': '22:00', 'price': 700,
    }

    def test_weekday_time_band_prices_each_step(self):
        self.assertEqual(self.put_rules([self.WEEKDAY_EVENING]).status_code, 200)
        # 16:00–18:00 = one hour at the default 600 + one hour at 700.
        response = self.book_on(self.monday, ['16:00 – 18:00'], 1320)
        self.assertEqual(response.status_code, 201, getattr(response, 'data', None))
        self.assertEqual(response.data['booking']['amount'], 1320)

    def test_stale_client_amount_is_refused_with_the_server_total(self):
        self.put_rules([self.WEEKDAY_EVENING])
        response = self.book_on(self.monday, ['18:00 – 20:00'], 1220)   # old 600/h price
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(response.data['expectedAmount'], 1420)

    def test_rule_does_not_apply_on_other_days(self):
        self.put_rules([self.WEEKDAY_EVENING])
        self.assertEqual(self.book_on(self.saturday, ['18:00 – 20:00'], 1220).status_code, 201)

    def test_existing_weekend_price_is_the_default_on_weekends(self):
        self.listing.record = {**self.listing.record, 'weekendPrice': 900}
        self.listing.save()
        self.put_rules([{
            'label': 'Weekend evening', 'kind': 'weekly', 'days': [5, 6],
            'start': '17:00', 'end': '22:00', 'price': 1000,
        }])
        # 16:00–18:00 Saturday: 1h at the weekend default 900 + 1h at 1000.
        self.assertEqual(self.book_on(self.saturday, ['16:00 – 18:00'], 1920).status_code, 201)

    def test_all_day_weekday_rule_replaces_the_default(self):
        self.put_rules([{'label': 'Weekdays', 'kind': 'weekly', 'days': [0, 1, 2, 3, 4], 'price': 500}])
        self.assertEqual(self.book_on(self.monday, ['10:00 – 12:00'], 1020).status_code, 201)

    def test_timed_rule_beats_all_day_rule_and_date_rule_beats_both(self):
        self.put_rules([
            {'label': 'Weekdays', 'kind': 'weekly', 'days': [0, 1, 2, 3, 4], 'price': 500},
            self.WEEKDAY_EVENING,
            {'label': 'Festival', 'kind': 'dates', 'from': self.monday.isoformat(),
             'to': self.monday.isoformat(), 'price': 1200},
        ])
        # Tuesday 16–18: 1h all-day weekday (500) + 1h timed evening (700).
        self.assertEqual(self.book_on(self.tuesday, ['16:00 – 18:00'], 1220).status_code, 201)
        # Monday is the festival date: every hour 1200.
        self.assertEqual(self.book_on(self.monday, ['16:00 – 18:00'], 2420).status_code, 201)

    def test_half_hour_bookings_split_across_bands(self):
        self.put_rules([self.WEEKDAY_EVENING])
        # 16:30–17:30: 30 min at 600 + 30 min at 700 = 650.
        self.assertEqual(self.book_on(self.monday, ['16:30 – 17:30'], 670).status_code, 201)

    def test_packages_are_never_repriced_by_rules(self):
        self.put_rules([{'label': 'Weekdays', 'kind': 'weekly', 'days': [0, 1, 2, 3, 4], 'price': 5}])
        response = self.book_on(
            self.monday, ['10:00 – 12:00'], 5020,
            addons=[{'name': 'Package — Birthday Deluxe', 'qty': 1, 'price': 5000}],
        )
        self.assertEqual(response.status_code, 201)

    def test_inactive_rules_are_ignored(self):
        self.put_rules([{**self.WEEKDAY_EVENING, 'active': False}])
        self.assertEqual(self.book_on(self.monday, ['18:00 – 20:00'], 1220).status_code, 201)

    def test_expired_date_rule_does_not_apply(self):
        past = today_ist() - datetime.timedelta(days=3)
        self.put_rules([{'label': 'Old', 'kind': 'dates', 'from': past.isoformat(),
                         'to': past.isoformat(), 'price': 50}])
        self.assertEqual(self.book_on(self.monday, ['18:00 – 20:00'], 1220).status_code, 201)

    def test_availability_publishes_the_price_bands(self):
        self.put_rules([self.WEEKDAY_EVENING])
        bands = self.availability(self.monday)['priceBands']
        self.assertEqual(
            [(b['start'], b['end'], b['rate'], b['label']) for b in bands],
            [('06:00', '17:00', None, None), ('17:00', '22:00', 700, 'Weekday evening'),
             ('22:00', '24:00', None, None)],
        )

    def test_walk_ins_follow_the_rules_too(self):
        self.put_rules([self.WEEKDAY_EVENING])
        self.as_vendor()
        response = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.listing.id), 'date': self.monday.isoformat(),
            'slots': ['16:00 – 18:00'], 'perSlot': 600, 'amount': 1300,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['booking']['amount'], 1300)


class PriceRuleValidationTests(BusinessTestBase):
    def assertRefused(self, rules, code=None):
        response = self.put_rules(rules)
        self.assertEqual(response.status_code, 400, response.data)
        if code:
            self.assertEqual(response.data['code'], code)
        return response

    def test_overlapping_rules_of_the_same_kind_are_refused(self):
        response = self.assertRefused([
            {'label': 'A', 'kind': 'weekly', 'days': [0, 1], 'start': '17:00', 'end': '22:00', 'price': 700},
            {'label': 'B', 'kind': 'weekly', 'days': [1, 2], 'start': '20:00', 'end': '23:00', 'price': 800},
        ], code='OVERLAPPING_RULES')
        self.assertIn('“A”', response.data['message'])
        self.assertIn('“B”', response.data['message'])

    def test_overlapping_date_rules_are_refused(self):
        self.assertRefused([
            {'label': 'Diwali', 'kind': 'dates', 'from': '2030-11-01', 'to': '2030-11-05', 'price': 900},
            {'label': 'Week', 'kind': 'dates', 'from': '2030-11-04', 'to': '2030-11-10', 'price': 800},
        ], code='OVERLAPPING_RULES')

    def test_non_overlapping_and_nested_rules_are_accepted(self):
        response = self.put_rules([
            {'label': 'Morning', 'kind': 'weekly', 'days': [0], 'start': '06:00', 'end': '12:00', 'price': 400},
            {'label': 'Evening', 'kind': 'weekly', 'days': [0], 'start': '12:00', 'end': '24:00', 'price': 800},
            {'label': 'All Monday', 'kind': 'weekly', 'days': [0], 'price': 600},   # all day: lower precedence
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['priceRules']), 3)

    def test_inactive_rules_may_overlap(self):
        response = self.put_rules([
            {'label': 'A', 'kind': 'weekly', 'days': [0], 'price': 700},
            {'label': 'B', 'kind': 'weekly', 'days': [0], 'price': 800, 'active': False},
        ])
        self.assertEqual(response.status_code, 200)

    def test_invalid_values_are_refused(self):
        bad = [
            {'kind': 'weekly', 'days': [0], 'price': 0},
            {'kind': 'weekly', 'days': [0], 'price': -5},
            {'kind': 'weekly', 'days': [0], 'price': 'abc'},
            {'kind': 'weekly', 'days': [], 'price': 500},
            {'kind': 'weekly', 'days': [7], 'price': 500},
            {'kind': 'weekly', 'days': [0], 'start': '17:00', 'price': 500},
            {'kind': 'weekly', 'days': [0], 'start': '18:00', 'end': '17:00', 'price': 500},
            {'kind': 'weekly', 'days': [0], 'start': '17:15', 'end': '18:00', 'price': 500},
            {'kind': 'weekly', 'days': [0], 'start': '04:00', 'end': '08:00', 'price': 500},
            {'kind': 'dates', 'from': '2030-01-05', 'to': '2030-01-01', 'price': 500},
            {'kind': 'dates', 'from': 'tomorrow', 'price': 500},
            {'kind': 'monthly', 'price': 500},
        ]
        for rule in bad:
            with self.subTest(rule=rule):
                self.assertRefused([rule])

    def test_resources_must_belong_to_the_venue(self):
        other = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='elsewhere', record={'name': 'Else', 'price': 1},
            name='Else', category='hall', locality='x', pincode='560001',
        )
        self.assertRefused([{'kind': 'weekly', 'days': [0], 'price': 500, 'resources': [str(other.id)]}])


class PitchRuleTests(BusinessTestBase):
    """A venue with a second pitch listing (detail.unitOf)."""

    def setUp(self):
        super().setUp()
        self.pitch2 = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.vendor, slug='grand-palace-hall-pitch-2',
            record={'name': 'Grand Palace Hall — Pitch 2', 'price': 800,
                    'detail': {'unitOf': str(self.listing.id), 'unitLabel': 'Pitch 2'}},
            name='Grand Palace Hall — Pitch 2', category='hall', locality='x', pincode='560001',
        )

    def test_rules_scoped_to_one_pitch(self):
        self.put_rules([{'label': 'Pitch 2 promo', 'kind': 'weekly', 'days': [0, 1, 2, 3, 4, 5, 6],
                         'price': 300, 'resources': [str(self.pitch2.id)]}])
        self.assertEqual(self.book_on(self.monday, ['10:00 – 11:00'], 620).status_code, 201)
        self.assertEqual(
            self.book_on(self.monday, ['10:00 – 11:00'], 320, listing=self.pitch2).status_code, 201,
        )

    def test_pitch_specific_rule_beats_venue_wide_rule(self):
        response = self.put_rules([
            {'label': 'All', 'kind': 'weekly', 'days': [0], 'price': 500},
            {'label': 'Pitch 2', 'kind': 'weekly', 'days': [0], 'price': 450,
             'resources': [str(self.pitch2.id)]},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.book_on(self.monday, ['10:00 – 11:00'], 520).status_code, 201)
        self.assertEqual(
            self.book_on(self.monday, ['10:00 – 11:00'], 470, listing=self.pitch2).status_code, 201,
        )

    def test_settings_are_shared_and_reachable_through_a_pitch_id(self):
        self.as_vendor()
        data = self.client.get(self.url('business', self.pitch2)).data
        self.assertEqual(data['venue']['id'], str(self.listing.id))
        labels = {r['id']: r['label'] for r in data['resources']}
        self.assertEqual(labels[str(self.pitch2.id)], 'Pitch 2')
        self.assertEqual(labels[str(self.listing.id)], 'Pitch 1')
        # Hours set on the venue close the pitch too.
        self.put_schedule(hours=week(**{str(self.monday.weekday()): []}))
        response = self.book_on(self.monday, ['10:00 – 11:00'], 820, listing=self.pitch2)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'VENUE_CLOSED')


class ExistingBookingTests(BusinessTestBase):
    def test_confirmed_bookings_keep_their_price_after_a_rule_change(self):
        booking = self.book_on(self.monday, ['18:00 – 20:00'], 1220)
        self.assertEqual(booking.status_code, 201)
        self.put_rules([{'label': 'Surge', 'kind': 'weekly', 'days': [0], 'price': 2000}])
        row = Booking.objects.get(pk=booking.data['booking']['id'])
        self.assertEqual(row.amount, 1220)
        self.assertEqual(row.status, 'confirmed')
        self.as_vendor()
        mine = self.client.get('/api/vendors/me/dashboard').data['allBookings']
        self.assertEqual([b['amount'] for b in mine], [1220])

    def test_a_pending_payment_confirms_at_the_amount_it_was_ordered_at(self):
        from bookings.payments import _confirm_booking
        order = self.order_on(self.monday, ['18:00 – 20:00'], 1220)
        self.assertEqual(order.status_code, 201)
        self.put_rules([{'label': 'Surge', 'kind': 'weekly', 'days': [0], 'price': 2000}])
        held = Booking.objects.get(pk=order.data['bookingId'])
        _confirm_booking(held, 'pay_X')
        held.refresh_from_db()
        self.assertEqual((held.status, held.amount, held.online_amount), ('confirmed', 1220, 1220))

    def order_on(self, date, slots, amount):
        from unittest.mock import MagicMock, patch
        fake = MagicMock(status_code=200)
        fake.json.return_value = {'id': f'order_{uuid.uuid4().hex[:8]}'}
        with patch('bookings.razorpay_client.requests.post', return_value=fake):
            return self.client.post('/api/payments/order', {
                'venueId': str(self.listing.id), 'date': date.isoformat(),
                'slots': slots, 'addons': [], 'perSlot': 600, 'amount': amount,
            }, format='json')


class OpeningHoursTests(BusinessTestBase):
    def test_closed_weekday_refuses_bookings(self):
        self.assertEqual(self.put_schedule(hours=week(**{'0': []})).status_code, 200)
        response = self.book_on(self.monday, ['10:00 – 11:00'], 620)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'VENUE_CLOSED')
        data = self.availability(self.monday)
        self.assertTrue(data['closed'])
        self.assertEqual(data['open'], [])
        self.assertEqual(data['unavailable'], [{'start': '06:00', 'end': '24:00', 'reason': 'closed'}])

    def test_outside_hours_is_refused_and_inside_is_fine(self):
        self.put_schedule(hours=week(**{'0': [{'start': '09:00', 'end': '13:00'},
                                              {'start': '16:00', 'end': '22:00'}]}))
        for slots in (['07:00 – 08:00'], ['12:00 – 14:00'], ['21:00 – 23:00']):
            with self.subTest(slots=slots):
                response = self.book_on(self.monday, slots, 620 if slots[0][:2] == '07' else 1220)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data['code'], 'OUTSIDE_HOURS')
        self.assertEqual(self.book_on(self.monday, ['09:00 – 10:00'], 620).status_code, 201)
        self.assertEqual(self.book_on(self.monday, ['16:00 – 18:00'], 1220).status_code, 201)
        unavailable = self.availability(self.monday)['unavailable']
        self.assertEqual(
            [(u['start'], u['end']) for u in unavailable],
            [('06:00', '09:00'), ('13:00', '16:00'), ('22:00', '24:00')],
        )

    def test_touching_periods_count_as_one(self):
        self.put_schedule(hours=week(**{'0': [{'start': '09:00', 'end': '13:00'},
                                              {'start': '13:00', 'end': '22:00'}]}))
        self.assertEqual(self.book_on(self.monday, ['12:00 – 14:00'], 1220).status_code, 201)

    def test_extra_hours_must_fit_too(self):
        self.listing.record = {**self.listing.record, 'detail': {
            **self.listing.record['detail'], 'extraHourPrice': '300', 'maxExtraHours': '2'}}
        self.listing.save()
        self.put_schedule(hours=week(**{'0': [{'start': '09:00', 'end': '20:00'}]}))
        response = self.book_on(
            self.monday, ['18:00 – 19:00'], 920,
            addons=[{'name': 'Extra hour', 'qty': 2, 'price': 300}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'OUTSIDE_HOURS')

    def test_special_hours_replace_the_weekday(self):
        self.put_schedule(
            hours=week(**{'0': []}),
            specialHours=[{'date': self.monday.isoformat(),
                           'periods': [{'start': '10:00', 'end': '14:00'}], 'note': 'Open house'}],
        )
        self.assertEqual(self.book_on(self.monday, ['10:00 – 11:00'], 620).status_code, 201)
        self.assertEqual(self.book_on(self.monday, ['15:00 – 16:00'], 620).status_code, 400)

    def test_closures_close_every_day_in_range_with_the_reason(self):
        self.put_schedule(closures=[{'from': self.monday.isoformat(),
                                     'to': self.tuesday.isoformat(), 'reason': 'Turf relaying'}])
        for day in (self.monday, self.tuesday):
            response = self.book_on(day, ['10:00 – 11:00'], 620)
            self.assertEqual(response.status_code, 400)
            self.assertIn('Turf relaying', response.data['message'])
        data = self.availability(self.monday)
        self.assertEqual(data['closedReason'], 'Turf relaying')

    def test_walk_ins_are_not_bound_by_opening_hours(self):
        self.put_schedule(hours=week(**{'0': []}))
        self.as_vendor()
        response = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.listing.id), 'date': self.monday.isoformat(),
            'slots': ['10:00 – 11:00'], 'perSlot': 600, 'amount': 600,
        }, format='json')
        self.assertEqual(response.status_code, 201)

    def test_invalid_hours_are_refused(self):
        bad = [
            {'0': ALL_DAY},                                             # days missing
            week(**{'0': [{'start': '09:00', 'end': '13:00'}, {'start': '12:00', 'end': '15:00'}]}),
            week(**{'0': [{'start': '10:00', 'end': '09:00'}]}),
            week(**{'0': [{'start': '05:00', 'end': '09:00'}]}),
            week(**{'0': [{'start': '09:10', 'end': '10:00'}]}),
            week(**{'0': [{'start': '09:00', 'end': '10:00'}] * 5}),
        ]
        for hours in bad:
            with self.subTest(hours=hours):
                self.assertEqual(self.put_schedule(hours=hours).status_code, 400)

    def test_public_venue_page_shows_hours_and_instructions(self):
        self.put_schedule(hours=week(**{'6': []}), bookingInstructions='Carry ID. Shoes required.')
        self.client.force_authenticate(user=None)
        record = self.client.get(f'/api/venues/{self.listing.id}').data
        self.assertEqual(record['business']['hours']['6'], [])
        self.assertEqual(record['business']['bookingInstructions'], 'Carry ID. Shoes required.')
        self.assertEqual(
            self.availability(self.monday)['bookingInstructions'], 'Carry ID. Shoes required.',
        )


class ScheduleConflictTests(BusinessTestBase):
    def setUp(self):
        super().setUp()
        response = self.book_on(self.monday, ['07:00 – 08:00'], 620)
        self.assertEqual(response.status_code, 201)
        self.early = response.data['booking']['id']
        self.new_hours = week(**{'0': [{'start': '09:00', 'end': '22:00'}]})

    def test_preview_lists_the_affected_bookings_without_saving(self):
        self.as_vendor()
        response = self.client.post(self.url('business/schedule/preview'),
                                    {'revision': 0, 'hours': self.new_hours}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c['id'] for c in response.data['conflicts']], [self.early])
        self.assertFalse(VenueBusinessSettings.objects.exists())

    def test_saving_over_a_booking_needs_acknowledgement(self):
        response = self.put_schedule(hours=self.new_hours)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'BOOKING_CONFLICTS')
        self.assertEqual([c['id'] for c in response.data['conflicts']], [self.early])
        self.assertFalse(VenueBusinessSettings.objects.exists())

        response = self.put_schedule(hours=self.new_hours, ack=[self.early])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([b['id'] for b in response.data['keptBookings']], [self.early])
        booking = Booking.objects.get(pk=self.early)
        self.assertEqual((booking.status, booking.amount, booking.slots),
                         ('confirmed', 620, ['07:00 – 08:00']))

    def test_a_closure_over_a_booking_needs_acknowledgement_too(self):
        closures = [{'from': self.monday.isoformat(), 'to': self.monday.isoformat(), 'reason': 'Repairs'}]
        self.assertEqual(self.put_schedule(closures=closures).status_code, 409)
        self.assertEqual(self.put_schedule(closures=closures, ack=[self.early]).status_code, 200)

    def test_cancelled_bookings_never_count_as_conflicts(self):
        Booking.objects.filter(pk=self.early).update(status='cancelled')
        self.assertEqual(self.put_schedule(hours=self.new_hours).status_code, 200)

    def test_stale_revision_is_refused_with_the_latest_settings(self):
        self.assertEqual(self.put_schedule(bookingInstructions='v1').status_code, 200)
        self.as_vendor()
        response = self.client.put(self.url('business/schedule'),
                                   {'revision': 0, 'bookingInstructions': 'v2'}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'STALE')
        self.assertEqual(response.data['settings']['bookingInstructions'], 'v1')


class BlockTests(BusinessTestBase):
    def test_block_hides_the_time_from_customers_and_walk_ins(self):
        response = self.block(self.monday, '18:00', '20:00', reason='Private party')
        self.assertEqual(response.status_code, 201, response.data)
        unavailable = self.availability(self.monday)['unavailable']
        self.assertEqual(unavailable, [{'start': '18:00', 'end': '20:00', 'reason': 'blocked'}])
        self.assertNotIn('Private party', str(unavailable))     # the note stays private

        refused = self.book_on(self.monday, ['19:00 – 21:00'], 1220)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data['code'], 'SLOT_BLOCKED')
        self.as_vendor()
        walk_in = self.client.post('/api/vendors/me/walkin-bookings', {
            'venueId': str(self.listing.id), 'date': self.monday.isoformat(),
            'slots': ['19:00 – 20:00'], 'perSlot': 600, 'amount': 600,
        }, format='json')
        self.assertEqual(walk_in.status_code, 409)
        self.assertEqual(self.book_on(self.monday, ['20:00 – 21:00'], 620).status_code, 201)

    def test_reopening_a_block_makes_the_time_bookable(self):
        block_id = self.block(self.monday, '18:00', '20:00').data['block']['id']
        self.as_vendor()
        self.assertEqual(self.client.delete(self.url(f'blocks/{block_id}')).status_code, 200)
        self.assertFalse(SlotBlock.objects.exists())
        self.assertEqual(self.book_on(self.monday, ['18:00 – 20:00'], 1220).status_code, 201)

    def test_a_block_over_a_booking_or_a_live_hold_is_refused(self):
        self.assertEqual(self.book_on(self.monday, ['18:00 – 19:00'], 620).status_code, 201)
        response = self.block(self.monday, '17:00', '19:00')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'BOOKING_CONFLICTS')
        self.assertEqual(len(response.data['conflicts']), 1)

        hold = ExistingBookingTests.order_on(self, self.tuesday, ['10:00 – 11:00'], 620)
        self.assertEqual(hold.status_code, 201)
        self.assertEqual(self.block(self.tuesday, '10:00', '12:00').status_code, 409)
        self.assertFalse(SlotBlock.objects.exists())

    def test_overlapping_and_past_blocks_are_refused(self):
        self.assertEqual(self.block(self.monday, '18:00', '20:00').status_code, 201)
        self.assertEqual(self.block(self.monday, '19:00', '21:00').status_code, 400)
        yesterday = today_ist() - datetime.timedelta(days=1)
        self.assertEqual(self.block(yesterday, '18:00', '20:00').status_code, 400)

    def test_a_past_block_cannot_be_reopened(self):
        block = SlotBlock.objects.create(
            listing=self.listing, date=today_ist() - datetime.timedelta(days=1),
            start_minute=600, end_minute=660,
        )
        self.as_vendor()
        self.assertEqual(self.client.delete(self.url(f'blocks/{block.pk}')).status_code, 400)
        self.assertTrue(SlotBlock.objects.filter(pk=block.pk).exists())

    def test_late_payment_after_a_block_is_queued_for_refund(self):
        from django.utils import timezone
        from bookings.payments import _conflicts_excluding_self
        hold = ExistingBookingTests.order_on(self, self.monday, ['10:00 – 11:00'], 620)
        booking = Booking.objects.get(pk=hold.data['bookingId'])
        Booking.objects.filter(pk=booking.pk).update(
            created_at=timezone.now() - datetime.timedelta(minutes=45))   # hold expired
        self.assertEqual(self.block(self.monday, '10:00', '11:00').status_code, 201)
        booking.refresh_from_db()
        self.assertTrue(_conflicts_excluding_self(booking))

    def test_order_created_after_a_block_is_refused_under_the_lock(self):
        from unittest.mock import patch
        from venues import business

        real = business.check_bookable
        calls = {'n': 0}

        def first_passes(*args, **kwargs):
            # Validation passes (the block didn't exist yet); the second,
            # locked check sees the block a vendor saved in between.
            calls['n'] += 1
            if calls['n'] == 1:
                SlotBlock.objects.create(listing=self.listing, date=self.monday,
                                         start_minute=600, end_minute=720)
                return None
            return real(*args, **kwargs)

        with patch('venues.business.check_bookable', side_effect=first_passes), \
                patch('bookings.payments.check_bookable', side_effect=first_passes):
            response = ExistingBookingTests.order_on(self, self.monday, ['10:00 – 11:00'], 620)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'SLOT_BLOCKED')
        self.assertFalse(Booking.objects.exists())

    def test_calendar_shows_bookings_blocks_and_prices(self):
        self.put_rules([{'label': 'Evening', 'kind': 'weekly', 'days': [0], 'start': '17:00',
                         'end': '22:00', 'price': 900}])
        self.book_on(self.monday, ['10:00 – 11:00'], 620)
        self.block(self.monday, '18:00', '19:00', reason='Maintenance')
        self.as_vendor()
        data = self.client.get(self.url('calendar') + f'?date={self.monday.isoformat()}').data
        day = data['days'][0]
        resource = day['resources'][0]
        self.assertEqual(resource['bookings'][0]['slots'], ['10:00 – 11:00'])
        self.assertEqual(resource['bookings'][0]['customer'], 'Asha')
        blocked = [u for u in resource['unavailable'] if u['reason'] == 'blocked']
        self.assertEqual(blocked[0]['note'], 'Maintenance')
        rates = {(b['start'], b['end']): b['rate'] for b in resource['priceBands']}
        self.assertEqual(rates[('17:00', '22:00')], 900)
        self.assertEqual(rates[('06:00', '17:00')], 600)
        self.assertEqual(len(data['blocks']), 1)


class OwnershipTests(BusinessTestBase):
    def setUp(self):
        super().setUp()
        self.other_vendor = User.objects.create_user(
            phone='9000000009', name='Other', email='o@example.com', role=User.Role.VENDOR,
        )

    def test_another_vendor_gets_404_everywhere(self):
        self.as_vendor(self.other_vendor)
        for method, suffix, body in (
            ('get', 'business', None),
            ('put', 'business/schedule', {'revision': 0, 'hours': week()}),
            ('put', 'business/pricing', {'revision': 0, 'priceRules': []}),
            ('post', 'business/pricing/preview', {}),
            ('post', 'business/schedule/preview', {'revision': 0}),
            ('get', 'calendar', None),
            ('post', 'blocks', {'date': self.monday.isoformat(), 'start': '10:00', 'end': '11:00'}),
        ):
            with self.subTest(method=method, suffix=suffix):
                call = getattr(self.client, method)
                response = call(self.url(suffix), body, format='json') if body is not None else call(self.url(suffix))
                self.assertEqual(response.status_code, 404)
        self.assertFalse(VenueBusinessSettings.objects.exists())
        self.assertFalse(SlotBlock.objects.exists())

    def test_another_vendor_cannot_reopen_a_block(self):
        block_id = self.block(self.monday, '10:00', '11:00').data['block']['id']
        self.as_vendor(self.other_vendor)
        self.assertEqual(self.client.delete(self.url(f'blocks/{block_id}')).status_code, 404)
        self.assertTrue(SlotBlock.objects.filter(pk=block_id).exists())

    def test_customers_are_refused(self):
        self.as_customer()
        self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_a_pitch_pointing_at_another_vendors_venue_does_not_borrow_its_settings(self):
        self.put_rules([{'label': 'Promo', 'kind': 'weekly', 'days': [0], 'price': 100}])
        stray = Listing.objects.create(
            id=uuid.uuid4(), vendor=self.other_vendor, slug='stray',
            record={'name': 'Stray', 'price': 600,
                    'detail': {'unitOf': str(self.listing.id), 'unitLabel': 'Pitch 9'}},
            name='Stray', category='hall', locality='x', pincode='560001',
        )
        self.assertEqual(self.book_on(self.monday, ['10:00 – 11:00'], 620, listing=stray).status_code, 201)


class PricingPreviewTests(BusinessTestBase):
    def test_preview_of_unsaved_rules_saves_nothing(self):
        self.as_vendor()
        response = self.client.post(self.url('business/pricing/preview'), {
            'date': self.monday.isoformat(),
            'priceRules': [{'label': 'Night', 'kind': 'weekly', 'days': [0], 'start': '20:00',
                            'end': '24:00', 'price': 1000}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['defaultRate'], 600)
        self.assertEqual(
            [(b['start'], b['end'], b['rate']) for b in response.data['bands']],
            [('06:00', '20:00', 600), ('20:00', '24:00', 1000)],
        )
        self.assertFalse(VenueBusinessSettings.objects.exists())

    def test_preview_refuses_ambiguous_rules(self):
        self.as_vendor()
        response = self.client.post(self.url('business/pricing/preview'), {
            'priceRules': [{'kind': 'weekly', 'days': [0], 'price': 1},
                           {'kind': 'weekly', 'days': [0], 'price': 2}],
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'OVERLAPPING_RULES')
