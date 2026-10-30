"""
The ratings / homepage migrations (bookings 0019, events 0005, adminpanel
0003) — and the review-reply columns after them (bookings 0020) — must leave
every existing row exactly as it was.

Rows are written with the HISTORICAL models at the pre-change schema — a
venue, a paid booking, a stars-only rating, an old text banner and a promoted
event — then the database is migrated forward and each one is read back with
today's models: same ids, same values, the old rating still counted, the old
banner still served. Nothing is duplicated or deleted.
"""
import datetime
import uuid

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [
    ('bookings', '0018_booking_codes'),
    ('events', '0004_event_coordinates_order_checkin'),
    ('adminpanel', '0002_auditentry_target_id_payout_period_end_and_more'),
    # The offerings app (2026-10-30) builds on bookings 0023, so it cannot
    # sit at its latest migration while bookings is rolled back — it did not
    # exist before these changes either.
    ('offerings', None),
]
AFTER = [
    # 0020 (vendor replies) is today's Rating model: reading the migrated rows
    # back with it needs its columns, and an old rating must come out with
    # no reply. 0021/0022 + events 0006 (invoice numbers, paid_at) are
    # today's Booking / EventOrder: an old booking must come out with its id
    # and payment reference unchanged, a fresh invoice number, and NO
    # invented payment time.
    # 0023 (Booking.kind) is today's Booking too: an old booking must come
    # out as an ordinary slot booking (kind '').
    # 0024 (Booking.hold_expires_at) and events 0007 (nullable = unlimited
    # EventDate.capacity) are today's models as well: an old booking comes
    # out with no stored hold end (holds.py falls back to created_at).
    ('bookings', '0024_booking_hold_expires_at'),
    ('events', '0007_event_date_unlimited_capacity'),
    ('adminpanel', '0003_settings_homepage'),
    ('offerings', '0001_initial'),
]


class MigrationPreservesDataTests(TransactionTestCase):

    def migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        # Every other app stays at its latest migration, so the historical
        # models match the tables those apps really have.
        moving = {app for app, _ in targets}
        targets = targets + [
            node for node in executor.loader.graph.leaf_nodes() if node[0] not in moving
        ]
        executor.migrate(targets)
        # (app, None) = "not applied at all" is valid for migrate() but is not
        # a graph node, so it is left out of the state lookup.
        return executor.loader.project_state([t for t in targets if t[1] is not None]).apps

    def tearDown(self):
        # Leave the test database at the latest schema for the next test case.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_records_and_ids_survive_unchanged(self):
        old = self.migrate(BEFORE)
        User = old.get_model('accounts', 'User')
        Listing = old.get_model('venues', 'Listing')
        Booking = old.get_model('bookings', 'Booking')
        Rating = old.get_model('bookings', 'Rating')
        Event = old.get_model('events', 'Event')
        Settings = old.get_model('adminpanel', 'Settings')

        vendor = User.objects.create(phone='9000000801', name='V', email='v801@example.com', role='vendor')
        customer = User.objects.create(phone='9000000802', name='C', email='c802@example.com')
        listing_id = uuid.uuid4()
        record = {'name': 'Old Hall', 'price': 900, 'detail': {'capacity': '50'}}
        Listing.objects.create(id=listing_id, vendor=vendor, slug='old-hall', record=record,
                               name='Old Hall', category='Party hall', status='live', featured=True)
        Booking.objects.create(
            id='bk_3f8a92c1d04e', listing_id=listing_id, user=customer, venue_name='Old Hall',
            date=datetime.date(2026, 1, 5), slots=['10:00 – 11:00'], amount=920, fee=20,
            status='confirmed', razorpay_payment_id='pay_legacy',
        )
        Rating.objects.create(booking_id='bk_3f8a92c1d04e', listing_id=listing_id, user=customer, stars=4)
        banners = [{'id': 1700000000000, 'title': 'Old text banner', 'type': 'flat', 'value': 100,
                    'code': 'OLD100', 'from': '', 'to': ''}]
        Settings.objects.create(pk=1, booking_fee=20, banners=banners)
        event_id = uuid.uuid4()
        Event.objects.create(
            id=event_id, slug='old-night', name='Old Night', start_time=datetime.time(18),
            end_time=datetime.time(22), status='live', featured=True,
        )

        self.migrate(AFTER)

        from adminpanel.homepage import active_banners
        from adminpanel.models import Settings as NewSettings
        from bookings.models import Booking as NewBooking, Rating as NewRating
        from bookings.ratings import venue_rating
        from events.models import Event as NewEvent
        from venues.models import Listing as NewListing

        listing = NewListing.objects.get(pk=listing_id)
        self.assertEqual((listing.record, listing.status, listing.featured), (record, 'live', True))
        self.assertEqual(NewListing.objects.count(), 1)
        booking = NewBooking.objects.get(pk='bk_3f8a92c1d04e')                 # legacy id kept
        self.assertEqual((booking.amount, booking.status), (920, 'confirmed'))
        self.assertEqual(booking.razorpay_payment_id, 'pay_legacy')          # payment ref kept
        from bookings.invoices import financial_year, format_invoice_number
        self.assertEqual(booking.invoice_number,                             # numbered once
                         format_invoice_number(financial_year(booking.created_at), 1))
        self.assertIsNone(booking.paid_at)                                   # never guessed
        self.assertEqual(booking.kind, '')                                   # still a slot booking
        self.assertEqual(NewBooking.objects.count(), 1)
        rating = NewRating.objects.get(booking=booking)
        self.assertEqual((rating.stars, rating.review, rating.event_order_id, rating.hidden_at),
                         (4, '', None, None))
        self.assertEqual((rating.reply, rating.replied_at, rating.reply_hidden_at), ('', None, None))
        self.assertEqual(venue_rating(listing), (4.0, 1))                    # still counted
        settings_obj = NewSettings.objects.get(pk=1)
        self.assertEqual(settings_obj.banners, banners)                     # stored list untouched
        self.assertEqual([b['code'] for b in active_banners(settings_obj)], ['OLD100'])
        event = NewEvent.objects.get(pk=event_id)
        self.assertEqual((event.featured, event.featured_order, event.featured_from), (True, None, None))
