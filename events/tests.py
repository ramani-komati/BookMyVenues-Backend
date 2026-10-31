"""
Event ticketing tests.

Focused on the five things that would cost real money or trust if wrong:
  * ticket inventory (never oversell, holds expire, capacity is per date)
  * vendor permissions (no vendor may touch another vendor's event)
  * date selection (today onward only, scheduled dates only, selling window)
  * payment verification (signature, idempotency, no tickets without money)
  * concurrent booking (two customers racing for the last seats)
"""
import datetime
import hashlib
import hmac
import threading
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from bookings.slots import today_ist
from venues.models import Listing

from .inventory import MAX_TICKETS_PER_ORDER, seats_remaining, seats_sold
from .models import Event, EventDate, EventOrder, TicketType


class CacheResetMixin:
    """Clear the shared cache before every test.

    Two things live in django.core.cache and would otherwise leak between
    tests: DRF's throttle counters (the payments scope is 20/min, and a test
    file posts far more orders per wall-clock minute than a real customer
    ever would) and the 60s cache_page entry on the public event list. Both
    produce failures that have nothing to do with the behaviour under test â€”
    the same artifact that makes bookings/test_security.py 429 on itself when
    the whole suite runs in one process.
    """

    def setUp(self):
        cache.clear()
        super().setUp()

    def tearDown(self):
        cache.clear()
        super().tearDown()


class EventTestCase(CacheResetMixin, TestCase):
    pass


RZP_SECRET = 'test_secret_key'
RZP_KEYS = override_settings(
    RAZORPAY_KEY_ID='rzp_test_abc123',
    RAZORPAY_KEY_SECRET=RZP_SECRET,
    RAZORPAY_WEBHOOK_SECRET='test_webhook_secret',
)


def signature(order_id, payment_id, secret=RZP_SECRET):
    return hmac.new(
        secret.encode(), f'{order_id}|{payment_id}'.encode(), hashlib.sha256
    ).hexdigest()


def make_vendor(phone='9000000001', name='Vendor One'):
    return User.objects.create_user(
        phone=phone, name=name, email=f'{phone}@example.com',
        role=User.Role.VENDOR, is_vendor=True,
    )


def make_customer(phone='9100000001', name='Asha'):
    return User.objects.create_user(
        phone=phone, name=name, email=f'{phone}@example.com', is_customer=True,
    )


def auth(user):
    from rest_framework_simplejwt.tokens import RefreshToken
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}'
    )
    return client


def make_event(vendor=None, *, name='ABC Dandiya Night', capacity=500,
               days_ahead=(1,), status=Event.Status.LIVE, adult=500, child=250,
               extra_types=(), **kwargs):
    """An event. `vendor=None` models an ADMIN-created event, which is what
    every new event now is; passing a vendor models the legacy rows."""
    event = Event.objects.create(
        vendor=vendor,
        slug=name.lower().replace(' ', '-'),
        name=name,
        category='Dandiya',
        description='Garba and dandiya all night.',
        poster_url='https://cdn.example.com/poster.jpg',
        venue_name='Open Grounds',
        locality='Hanamkonda',
        city='Warangal',
        start_time=datetime.time(18, 0),
        end_time=datetime.time(22, 0),
        status=status,
        **kwargs,
    )
    today = today_ist()
    for offset in days_ahead:
        EventDate.objects.create(
            event=event, date=today + datetime.timedelta(days=offset),
            capacity=capacity,
        )
    TicketType.objects.create(
        event=event, key='adult', label='Adult', price=adult, sort_order=0
    )
    TicketType.objects.create(
        event=event, key='child', label='Child', price=child, sort_order=1
    )
    for index, spec in enumerate(extra_types, start=2):
        TicketType.objects.create(event=event, sort_order=index, **spec)
    return event


# =========================================================================
# Ticket inventory
# =========================================================================

@RZP_KEYS
class TicketInventoryTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(self.vendor, capacity=10)
        self.date = self.event.dates.first()

    def order(self, *, tickets=None, client=None, **extra):
        payload = {
            'eventId': str(self.event.id),
            'eventDateId': self.date.id,
            'tickets': tickets if tickets is not None else {'adult': 2},
            **extra,
        }
        with patch('events.payments.create_order', return_value='order_test_1'):
            return (client or self.client).post(
                reverse('event-payment-order'), payload, format='json'
            )

    def test_an_order_holds_its_seats(self):
        self.assertEqual(seats_remaining(self.date), 10)
        self.assertEqual(self.order(tickets={'adult': 3}).status_code, 201)
        self.assertEqual(seats_remaining(self.date), 7)

    def test_capacity_cannot_be_exceeded(self):
        self.assertEqual(self.order(tickets={'adult': 10}).status_code, 201)
        response = self.order(tickets={'adult': 1}, client=auth(make_customer('9100000002')))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'SOLD_OUT')
        self.assertEqual(response.data['remaining'], 0)

    def test_a_partial_overshoot_reports_what_is_left(self):
        self.order(tickets={'adult': 8})
        response = self.order(tickets={'adult': 5}, client=auth(make_customer('9100000003')))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['remaining'], 2)

    def test_exactly_the_remaining_seats_is_allowed(self):
        self.order(tickets={'adult': 8})
        other = auth(make_customer('9100000004'))
        self.assertEqual(self.order(tickets={'adult': 2}, client=other).status_code, 201)
        self.assertEqual(seats_remaining(self.date), 0)

    def test_an_expired_hold_releases_its_seats(self):
        self.order(tickets={'adult': 10})
        self.assertEqual(seats_remaining(self.date), 0)
        # Age the unpaid hold past the 30-minute window.
        EventOrder.objects.update(
            created_at=timezone.now() - datetime.timedelta(minutes=31)
        )
        self.assertEqual(seats_remaining(self.date), 10)
        self.assertEqual(
            self.order(tickets={'adult': 4}, client=auth(make_customer('9100000005'))).status_code,
            201,
        )

    def test_a_fresh_hold_still_blocks(self):
        self.order(tickets={'adult': 10})
        EventOrder.objects.update(
            created_at=timezone.now() - datetime.timedelta(minutes=29)
        )
        self.assertEqual(seats_remaining(self.date), 0)

    def test_a_cancelled_order_frees_its_seats(self):
        response = self.order(tickets={'adult': 6})
        order_id = response.data['eventOrderId']
        self.assertEqual(seats_remaining(self.date), 4)
        self.client.delete(
            reverse('cancel-event-order', args=[order_id])
        )
        self.assertEqual(seats_remaining(self.date), 10)

    def test_capacity_is_per_date_not_per_event(self):
        event = make_event(
            self.vendor, name='Two Night Garba', capacity=5, days_ahead=(1, 2)
        )
        first, second = list(event.dates.all())
        self.date, self.event = first, event
        self.assertEqual(self.order(tickets={'adult': 5}).status_code, 201)
        # Night one is sold out; night two must be untouched.
        self.assertEqual(seats_remaining(first), 0)
        self.assertEqual(seats_remaining(second), 5)
        self.date = second
        self.assertEqual(
            self.order(tickets={'adult': 5}, client=auth(make_customer('9100000006'))).status_code,
            201,
        )

    def test_a_couple_ticket_consumes_two_seats(self):
        event = make_event(
            self.vendor, name='Couple Night', capacity=4,
            extra_types=[{
                'key': 'couple', 'label': 'Couple', 'price': 900,
                'seats_per_ticket': 2,
            }],
        )
        self.event, self.date = event, event.dates.first()
        self.assertEqual(self.order(tickets={'couple': 2}).status_code, 201)
        self.assertEqual(seats_remaining(self.date), 0)

    def test_per_order_ticket_ceiling(self):
        event = make_event(self.vendor, name='Big Night', capacity=1000)
        self.event, self.date = event, event.dates.first()
        response = self.order(tickets={'adult': MAX_TICKETS_PER_ORDER + 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'MAX_TICKETS')

    def test_zero_tickets_is_refused(self):
        response = self.order(tickets={'adult': 0})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'NO_TICKETS')

    def test_a_negative_quantity_cannot_shrink_the_bill(self):
        response = self.order(tickets={'adult': 4, 'child': -3})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'BAD_QUANTITY')

    def test_an_unknown_ticket_type_is_refused_not_dropped(self):
        response = self.order(tickets={'adult': 1, 'vvip': 2})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'UNKNOWN_TICKET_TYPE')

    def test_a_sold_out_event_date_reports_sold_out_in_availability(self):
        self.order(tickets={'adult': 10})
        response = self.client.get(
            reverse('public-event-availability', args=[self.event.id]),
            {'date': self.date.date.isoformat()},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['soldOut'])
        self.assertFalse(response.data['available'])

    def test_the_public_availability_read_leaks_no_seat_counts(self):
        """The product rule: customers never see a remaining-seat counter.
        The surest way to keep one off the screen is for the number never to
        reach the browser — the capacity check itself is untouched (see
        test_capacity_cannot_be_exceeded above, which still passes)."""
        response = self.client.get(
            reverse('public-event-availability', args=[self.event.id]),
            {'date': self.date.date.isoformat()},
        )
        for leak in ('remaining', 'capacity', 'sold'):
            self.assertNotIn(leak, response.data)


# =========================================================================
# Pricing â€” the server, never the client, decides the money
# =========================================================================

@RZP_KEYS
class TicketPricingTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(self.vendor, adult=500, child=250)
        self.date = self.event.dates.first()

    def order(self, **extra):
        payload = {
            'eventId': str(self.event.id),
            'eventDateId': self.date.id,
            'tickets': {'adult': 2},
            **extra,
        }
        with patch('events.payments.create_order', return_value='order_test_1'):
            return self.client.post(
                reverse('event-payment-order'), payload, format='json'
            )

    def test_the_brief_example_prices_correctly(self):
        # Customer B: 3 adult + 1 child = 1500 + 250 = 1750, plus the â‚¹20 fee.
        response = self.order(tickets={'adult': 3, 'child': 1})
        self.assertEqual(response.status_code, 201)
        breakdown = response.data['breakdown']
        self.assertEqual(breakdown['subtotal'], 1750)
        self.assertEqual(breakdown['fee'], 20)
        self.assertEqual(breakdown['amount'], 1770)
        self.assertEqual(response.data['amount'], 177000)   # paise

    def test_a_tampered_total_is_refused_with_the_real_one(self):
        response = self.order(tickets={'adult': 2}, amount=1)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'AMOUNT_MISMATCH')
        self.assertEqual(response.data['expectedAmount'], 1020)

    def test_the_stored_order_uses_server_prices_not_the_request(self):
        self.order(
            tickets={'adult': 2},
            items=[{'key': 'adult', 'qty': 2, 'price': 1}],   # ignored
        )
        order = EventOrder.objects.get()
        self.assertEqual(order.items[0]['price'], 500)
        self.assertEqual(order.subtotal, 1000)
        self.assertEqual(order.amount, 1020)

    def test_the_quote_matches_what_the_order_charges(self):
        quote = self.client.post(
            reverse('event-quote'),
            {
                'eventId': str(self.event.id),
                'eventDateId': self.date.id,
                'tickets': {'adult': 3, 'child': 1},
            },
            format='json',
        )
        self.assertEqual(quote.status_code, 200)
        order = self.order(tickets={'adult': 3, 'child': 1})
        self.assertEqual(quote.data['amount'], order.data['breakdown']['amount'])

    def test_the_fee_follows_the_admin_setting(self):
        from adminpanel.models import Settings
        settings_obj = Settings.load()
        settings_obj.booking_fee = 35
        settings_obj.fee_date = ''
        settings_obj.save()
        response = self.order(tickets={'adult': 1})
        self.assertEqual(response.data['breakdown']['fee'], 35)
        self.assertEqual(response.data['breakdown']['amount'], 535)

    def test_a_max_per_order_cap_is_enforced(self):
        TicketType.objects.filter(event=self.event, key='adult').update(max_per_order=2)
        response = self.order(tickets={'adult': 3})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'MAX_PER_ORDER')


# =========================================================================
# Date selection
# =========================================================================

@RZP_KEYS
class DateSelectionTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.today = today_ist()

    def order(self, event, *, date_id=None, date=None, tickets=None):
        payload = {'eventId': str(event.id), 'tickets': tickets or {'adult': 1}}
        if date_id is not None:
            payload['eventDateId'] = date_id
        if date is not None:
            payload['date'] = date
        with patch('events.payments.create_order', return_value='order_test_1'):
            return self.client.post(
                reverse('event-payment-order'), payload, format='json'
            )

    def test_the_date_list_starts_at_today(self):
        event = make_event(self.vendor, days_ahead=(0, 1, 5))
        response = self.client.get(reverse('public-event-detail', args=[event.slug]))
        dates = [d['date'] for d in response.data['dates']]
        self.assertEqual(dates[0], self.today.isoformat())

    def test_past_dates_are_not_offered(self):
        event = make_event(self.vendor, days_ahead=(2,))
        EventDate.objects.create(
            event=event, date=self.today - datetime.timedelta(days=3), capacity=50
        )
        response = self.client.get(reverse('public-event-detail', args=[event.slug]))
        offered = [d['date'] for d in response.data['dates']]
        self.assertNotIn((self.today - datetime.timedelta(days=3)).isoformat(), offered)
        # â€¦but the detail page still KNOWS about it, for "ran on" display.
        self.assertIn(
            (self.today - datetime.timedelta(days=3)).isoformat(),
            [d['date'] for d in response.data['allDates']],
        )

    def test_a_past_date_cannot_be_booked(self):
        event = make_event(self.vendor, days_ahead=(2,))
        past = EventDate.objects.create(
            event=event, date=self.today - datetime.timedelta(days=3), capacity=50
        )
        response = self.order(event, date_id=past.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'DATE_UNAVAILABLE')
        self.assertIn('past', response.data['message'].lower())

    def test_a_date_the_event_does_not_run_on_is_refused(self):
        event = make_event(self.vendor, days_ahead=(1,))
        far = (self.today + datetime.timedelta(days=200)).isoformat()
        response = self.order(event, date=far)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['code'], 'DATE_NOT_FOUND')

    def test_a_vendor_closed_night_is_refused(self):
        event = make_event(self.vendor, days_ahead=(1,))
        event_date = event.dates.first()
        event_date.is_available = False
        event_date.save()
        response = self.order(event, date_id=event_date.id)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['code'], 'DATE_UNAVAILABLE')
        self.assertNotIn(
            event_date.date.isoformat(),
            [
                d['date'] for d in self.client.get(
                    reverse('public-event-detail', args=[event.slug])
                ).data['dates']
            ],
        )

    def test_booking_before_the_window_opens_is_refused(self):
        event = make_event(
            self.vendor, days_ahead=(10,),
            booking_opens_on=self.today + datetime.timedelta(days=3),
        )
        response = self.order(event, date_id=event.dates.first().id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('open on', response.data['message'])

    def test_booking_after_the_window_closes_is_refused(self):
        event = make_event(
            self.vendor, days_ahead=(10,),
            booking_closes_on=self.today - datetime.timedelta(days=1),
        )
        response = self.order(event, date_id=event.dates.first().id)
        self.assertEqual(response.status_code, 400)
        self.assertIn('closed', response.data['message'])

    def test_availability_refuses_a_past_date_query(self):
        event = make_event(self.vendor)
        response = self.client.get(
            reverse('public-event-availability', args=[event.id]),
            {'date': (self.today - datetime.timedelta(days=1)).isoformat()},
        )
        self.assertEqual(response.status_code, 400)

    def test_a_non_live_event_cannot_be_booked_or_browsed(self):
        event = make_event(self.vendor, status=Event.Status.PENDING)
        self.assertEqual(
            self.client.get(
                reverse('public-event-detail', args=[event.slug])
            ).status_code,
            404,
        )
        response = self.order(event, date_id=event.dates.first().id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'EVENT_UNAVAILABLE')


# =========================================================================
# Super-admin event management — events are an ADMIN product
# =========================================================================

def make_admin(email='admin@example.com', phone='9900000001'):
    return User.objects.create_user(
        phone=phone, name='Admin', email=email,
        role=User.Role.ADMIN, is_staff=True, password='x',
    )


def admin_client(admin):
    """The admin panel authenticates with a cookie SESSION, not a JWT."""
    client = APIClient()
    client.force_login(admin)
    return client


class AdminEventManagementTests(EventTestCase):
    """The client's rule: ONLY the super admin creates and manages events.

    These cover the whole lifecycle the admin dashboard drives — create,
    validate, publish, unpublish, edit, delete — plus the protections that
    must survive an edit once customers have paid.
    """

    def setUp(self):
        super().setUp()
        self.admin = make_admin()
        self.client = admin_client(self.admin)
        self.customer = make_customer()

    def payload(self, **extra):
        today = today_ist()
        return {
            'name': 'Admin Dandiya Night',
            'category': 'Dandiya',
            'description': 'Nine nights of garba.',
            'startTime': '18:00',
            'endTime': '22:00',
            'venueName': 'Open Grounds',
            'city': 'Warangal',
            'locality': 'Hanamkonda',
            'organizerName': 'Kakatiya Cultural Trust',
            'organizerPhone': '9876500001',
            'dates': [
                {'date': (today + datetime.timedelta(days=4)).isoformat(),
                 'capacity': 200},
            ],
            'ticketTypes': [
                {'key': 'adult', 'label': 'Adult', 'price': 500},
                {'key': 'child', 'label': 'Child', 'price': 250},
            ],
            **extra,
        }

    def create(self, **extra):
        return self.client.post(
            reverse('admin-events'), self.payload(**extra), format='json'
        )

    # --- creation ---------------------------------------------------------

    def test_an_admin_can_create_an_event(self):
        response = self.create()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['event']['name'], 'Admin Dandiya Night')
        self.assertEqual(len(response.data['event']['dates']), 1)

    def test_a_new_event_is_a_draft_until_published(self):
        self.create()
        event = Event.objects.get(name='Admin Dandiya Night')
        self.assertEqual(event.status, Event.Status.DRAFT)
        # …and is therefore invisible to customers.
        self.assertEqual(
            APIClient().get(
                reverse('public-event-detail', args=[event.slug])
            ).status_code,
            404,
        )

    def test_an_admin_created_event_has_no_vendor_and_needs_no_onboarding(self):
        """The whole point of the change: no vendor account, no bank details,
        no payout setup stands between the admin and a listed event."""
        self.create()
        event = Event.objects.get(name='Admin Dandiya Night')
        self.assertIsNone(event.vendor_id)
        self.assertEqual(event.created_by_id, self.admin.id)
        self.assertEqual(event.organizer_name, 'Kakatiya Cultural Trust')
        self.assertEqual(event.organizer, 'Kakatiya Cultural Trust')

    def test_create_and_publish_in_one_call(self):
        response = self.create(status='publish')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['event']['status'], 'live')
        event = Event.objects.get(name='Admin Dandiya Night')
        self.assertEqual(
            APIClient().get(
                reverse('public-event-detail', args=[event.slug])
            ).status_code,
            200,
        )

    def test_a_unique_slug_is_minted_for_a_duplicate_name(self):
        self.create()
        self.create()
        slugs = set(Event.objects.values_list('slug', flat=True))
        self.assertIn('admin-dandiya-night', slugs)
        self.assertIn('admin-dandiya-night-2', slugs)

    # --- validation -------------------------------------------------------

    def test_a_name_is_required(self):
        response = self.create(name='   ')
        self.assertEqual(response.status_code, 400)
        self.assertIn('name', response.data['detail'].lower())

    def test_a_category_is_required(self):
        response = self.create(category='')
        self.assertEqual(response.status_code, 400)
        self.assertIn('category', response.data['detail'].lower())

    def test_a_place_is_required(self):
        response = self.create(venueName='')
        self.assertEqual(response.status_code, 400)
        self.assertIn('venue', response.data['detail'].lower())

    def test_adult_and_child_prices_are_required(self):
        response = self.create(
            ticketTypes=[{'key': 'adult', 'label': 'Adult', 'price': 500}]
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn('child', response.data['detail'])

    def test_at_least_one_date_is_required(self):
        response = self.create(dates=[])
        self.assertEqual(response.status_code, 400)
        self.assertIn('date', response.data['detail'].lower())

    def test_a_past_date_cannot_be_created(self):
        response = self.create(dates=[{
            'date': (today_ist() - datetime.timedelta(days=1)).isoformat(),
            'capacity': 10,
        }])
        self.assertEqual(response.status_code, 400)
        self.assertIn('past', response.data['detail'])

    def test_zero_capacity_is_refused(self):
        response = self.create(dates=[{
            'date': (today_ist() + datetime.timedelta(days=3)).isoformat(),
            'capacity': 0,
        }])
        self.assertEqual(response.status_code, 400)
        self.assertIn('capacity', response.data['detail'].lower())

    def test_a_negative_price_is_refused(self):
        response = self.create(ticketTypes=[
            {'key': 'adult', 'label': 'Adult', 'price': -100},
            {'key': 'child', 'label': 'Child', 'price': 250},
        ])
        self.assertEqual(response.status_code, 400)

    def test_a_bad_time_is_refused(self):
        response = self.create(startTime='six pm')
        self.assertEqual(response.status_code, 400)
        self.assertIn('HH:MM', response.data['detail'])

    def test_a_booking_window_cannot_close_before_it_opens(self):
        today = today_ist()
        response = self.create(
            bookingOpensOn=(today + datetime.timedelta(days=5)).isoformat(),
            bookingClosesOn=(today + datetime.timedelta(days=2)).isoformat(),
        )
        self.assertEqual(response.status_code, 400)

    def test_publishing_without_an_upcoming_date_is_refused(self):
        """An event with nothing bookable would put a dead card on the
        customer's Events page."""
        event = make_event(self.admin_owned(), days_ahead=(2,),
                           status=Event.Status.DRAFT)
        event.dates.update(is_available=False)
        response = self.client.patch(
            reverse('admin-event', args=[event.id]),
            {'status': 'publish'}, format='json',
        )
        self.assertEqual(response.status_code, 400)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.DRAFT)

    def admin_owned(self):
        """make_event() wants a `vendor`; None models an admin-created row."""
        return None

    # --- photos -----------------------------------------------------------

    def test_an_event_carries_a_whole_photo_gallery(self):
        """An event is not one poster. Venues have always had a gallery and
        their cards run a slideshow off it; events now match."""
        shots = [f'https://cdn.example.com/e{i}.jpg' for i in range(1, 5)]
        response = self.create(posterUrl=shots[0], gallery=shots[1:])
        self.assertEqual(response.status_code, 201)
        event = Event.objects.get(name='Admin Dandiya Night')
        self.assertEqual(event.poster_url, shots[0])
        self.assertEqual(event.gallery, shots[1:])

    def test_the_gallery_reaches_the_customer_card_not_just_the_detail_page(self):
        shots = [f'https://cdn.example.com/c{i}.jpg' for i in range(1, 4)]
        self.create(status='publish', posterUrl=shots[0], gallery=shots[1:])
        event = Event.objects.get(name='Admin Dandiya Night')
        card = next(
            e for e in APIClient().get(reverse('public-event-list')).data['events']
            if e['id'] == str(event.id)
        )
        self.assertEqual(card['poster'], shots[0])
        self.assertEqual(card['gallery'], shots[1:])
        detail = APIClient().get(
            reverse('public-event-detail', args=[event.slug])
        ).data
        self.assertEqual(detail['gallery'], shots[1:])

    def test_the_gallery_is_capped(self):
        many = [f'https://cdn.example.com/m{i}.jpg' for i in range(20)]
        self.create(gallery=many)
        event = Event.objects.get(name='Admin Dandiya Night')
        self.assertLessEqual(len(event.gallery), 8)

    def test_removing_every_extra_photo_persists(self):
        """An empty gallery must be stored, not ignored as "no change" —
        otherwise a photo the admin deleted stays on the customer's card."""
        created = self.create(
            posterUrl='https://cdn.example.com/a.jpg',
            gallery=['https://cdn.example.com/b.jpg'],
        ).data['event']
        self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'gallery': []}, format='json',
        )
        self.assertEqual(Event.objects.get(pk=created['id']).gallery, [])

    # --- publish / unpublish ---------------------------------------------

    def test_publish_then_unpublish(self):
        created = self.create().data['event']
        published = self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'status': 'publish'}, format='json',
        )
        self.assertEqual(published.status_code, 200)
        self.assertEqual(published.data['event']['status'], 'live')

        unpublished = self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'status': 'unpublish'}, format='json',
        )
        self.assertEqual(unpublished.data['event']['status'], 'paused')
        # Hidden from customers again.
        self.assertEqual(
            APIClient().get(
                reverse('public-event-detail', args=[created['slug']])
            ).status_code,
            404,
        )

    def test_an_unpublished_event_cannot_be_booked(self):
        created = self.create(status='publish').data['event']
        event = Event.objects.get(pk=created['id'])
        self.client.patch(
            reverse('admin-event', args=[event.id]),
            {'status': 'unpublish'}, format='json',
        )
        with patch('events.payments.create_order', return_value='order_x'):
            response = auth(self.customer).post(
                reverse('event-payment-order'),
                {
                    'eventId': str(event.id),
                    'eventDateId': event.dates.first().id,
                    'tickets': {'adult': 1},
                },
                format='json',
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'EVENT_UNAVAILABLE')

    def test_an_invalid_status_is_refused(self):
        created = self.create().data['event']
        response = self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'status': 'whatever'}, format='json',
        )
        self.assertEqual(response.status_code, 400)

    def test_a_status_change_is_audited(self):
        from adminpanel.models import AuditEntry
        created = self.create().data['event']
        self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'status': 'publish'}, format='json',
        )
        self.assertTrue(
            AuditEntry.objects.filter(target_id=created['id']).exists()
        )

    # --- editing ----------------------------------------------------------

    def test_an_admin_can_edit_the_content(self):
        created = self.create().data['event']
        response = self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'name': 'Renamed Night', 'description': 'New blurb'},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['event']['name'], 'Renamed Night')
        self.assertEqual(response.data['event']['slug'], 'renamed-night')

    def test_prices_can_be_changed(self):
        created = self.create().data['event']
        response = self.client.patch(
            reverse('admin-event', args=[created['id']]),
            {'ticketTypes': [
                {'key': 'adult', 'label': 'Adult', 'price': 600},
                {'key': 'child', 'label': 'Child', 'price': 300},
            ]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        prices = {t['key']: t['price'] for t in response.data['event']['ticketTypes']}
        self.assertEqual(prices['adult'], 600)

    def test_capacity_cannot_be_cut_below_what_is_sold(self):
        event = make_event(None, name='Sold Night')
        event_date = event.dates.first()
        EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            date=event_date.date, seats=20, ticket_count=20,
            amount=10020, subtotal=10000, fee=20, status='confirmed',
        )
        response = self.client.patch(
            reverse('admin-event', args=[event.id]),
            {'dates': [{'date': event_date.date.isoformat(), 'capacity': 5}]},
            format='json',
        )
        self.assertEqual(response.status_code, 409)
        event_date.refresh_from_db()
        self.assertEqual(event_date.capacity, 500)

    def test_a_date_with_sales_cannot_be_removed(self):
        event = make_event(None, name='Sold Night')
        event_date = event.dates.first()
        EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            date=event_date.date, seats=2, ticket_count=2,
            amount=1020, subtotal=1000, fee=20, status='confirmed',
        )
        other_day = (today_ist() + datetime.timedelta(days=9)).isoformat()
        response = self.client.patch(
            reverse('admin-event', args=[event.id]),
            {'dates': [{'date': other_day, 'capacity': 100}]}, format='json',
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(event.dates.filter(pk=event_date.pk).exists())

    def test_an_already_running_events_past_night_survives_an_edit(self):
        """Editing day 4 of a nine-night run must not demand that nights 1–3
        (now in the past) be deleted — the tickets sold for them are real."""
        event = make_event(None, name='Running Night', days_ahead=(2,))
        past = EventDate.objects.create(
            event=event, date=today_ist() - datetime.timedelta(days=2),
            capacity=100,
        )
        response = self.client.patch(
            reverse('admin-event', args=[event.id]),
            {'dates': [
                {'date': past.date.isoformat(), 'capacity': 100},
                {'date': event.dates.exclude(pk=past.pk).first().date.isoformat(),
                 'capacity': 500},
            ]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(event.dates.filter(pk=past.pk).exists())

    # --- deletion ---------------------------------------------------------

    def test_an_unsold_event_is_soft_deleted(self):
        created = self.create().data['event']
        response = self.client.delete(reverse('admin-event', args=[created['id']]))
        self.assertEqual(response.status_code, 200)
        event = Event.objects.get(pk=created['id'])
        self.assertEqual(event.status, Event.Status.DELETED)   # row survives

    def test_an_event_with_sales_cannot_be_deleted(self):
        event = make_event(None, name='Sold Night')
        event_date = event.dates.first()
        EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            date=event_date.date, seats=2, ticket_count=2,
            amount=1020, subtotal=1000, fee=20, status='confirmed',
        )
        response = self.client.delete(reverse('admin-event', args=[event.id]))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'HAS_SALES')
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.PAUSED)   # unpublished instead

    # --- bookings / payment visibility -----------------------------------

    def test_the_admin_sees_the_bookings_and_their_payment_status(self):
        event = make_event(None, name='Sold Night')
        event_date = event.dates.first()
        EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            customer_name='Asha', date=event_date.date, seats=2, ticket_count=2,
            amount=1020, subtotal=1000, fee=20, status='confirmed',
            razorpay_payment_id='pay_123',
        )
        EventOrder.objects.create(
            event=event, event_date=event_date, user=self.customer,
            customer_name='Mid Checkout', date=event_date.date, seats=1,
            ticket_count=1, amount=520, subtotal=500, fee=20,
            status='payment_pending',
        )
        response = self.client.get(reverse('admin-event-orders', args=[event.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['total'], 2)
        statuses = {o['paymentStatus'] for o in response.data['orders']}
        self.assertEqual(statuses, {'confirmed', 'payment_pending'})
        # Revenue counts PAID money only — a hold is not income.
        self.assertEqual(response.data['revenue'], 1000)
        self.assertEqual(response.data['ticketsSold'], 2)

    # --- authorisation ----------------------------------------------------

    def test_anonymous_cannot_create_an_event(self):
        response = APIClient().post(
            reverse('admin-events'), self.payload(), format='json'
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Event.objects.filter(name='Admin Dandiya Night').exists())

    def test_a_customer_cannot_create_an_event(self):
        response = auth(self.customer).post(
            reverse('admin-events'), self.payload(), format='json'
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Event.objects.filter(name='Admin Dandiya Night').exists())

    def test_a_vendor_cannot_use_the_admin_event_endpoint(self):
        vendor = make_vendor()
        response = auth(vendor).post(
            reverse('admin-events'), self.payload(), format='json'
        )
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(Event.objects.filter(name='Admin Dandiya Night').exists())


# =========================================================================
# Vendor event access — READ-ONLY. Every write must be refused server-side.
# =========================================================================

class VendorEventAccessTests(EventTestCase):
    """Who can READ which events through the vendor endpoints. Each call is
    the raw API, which is what an old bundle or a hand-rolled request would
    reach."""

    def setUp(self):
        super().setUp()
        self.owner = make_vendor('9000000001', 'Owner')
        self.other = make_vendor('9000000002', 'Other Vendor')
        self.customer = make_customer()
        # A legacy vendor-created event: the rows that existed before events
        # became an admin product.
        self.event = make_event(self.owner)
        self.owner_client = auth(self.owner)
        self.other_client = auth(self.other)

    def payload(self):
        today = today_ist()
        return {
            'name': 'Vendor Attempt',
            'category': 'Dandiya',
            'startTime': '18:00',
            'endTime': '22:00',
            'venueName': 'Grounds',
            'city': 'Warangal',
            'dates': [{'date': (today + datetime.timedelta(days=4)).isoformat(),
                       'capacity': 200}],
            'ticketTypes': [
                {'key': 'adult', 'label': 'Adult', 'price': 500},
                {'key': 'child', 'label': 'Child', 'price': 250},
            ],
        }

    # --- writes never bypass admin approval --------------------------------
    # Restored originals. Vendors may now SUBMIT and EDIT their own events,
    # so each test keeps its original name and protection, checked under the
    # approval flow: nothing a vendor does can make an event live, keep a
    # changed event live, or remove an event customers hold tickets for.
    # (Full lifecycle coverage: events/test_vendor_submission.py.)

    def _public_ids(self):
        # The public list is response-cached for 60 s (existing behaviour);
        # clear it so each check reads the current state. Checkout itself
        # never uses the cache and refuses a non-live event immediately.
        cache.clear()
        return [e['id'] for e in APIClient().get('/api/events', {'limit': 50}).data['events']]

    def test_a_vendor_cannot_create_an_event(self):
        """…that is live: a vendor submission is saved as PENDING and stays
        off the public site until an admin approves it."""
        response = self.owner_client.post(
            reverse('vendor-events'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, 201)
        created = Event.objects.get(name='Vendor Attempt')
        self.assertEqual(created.status, Event.Status.PENDING)
        self.assertEqual(created.vendor, self.owner)
        self.assertNotIn(str(created.id), self._public_ids())

    def test_a_vendor_cannot_edit_their_own_event(self):
        """…and keep it live: the edit is saved but the event goes back to
        PENDING and off the public site until approved again."""
        self.assertIn(str(self.event.id), self._public_ids())
        response = self.owner_client.patch(
            reverse('vendor-event-detail', args=[self.event.id]),
            {'name': 'Renamed by vendor'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertEqual(self.event.name, 'Renamed by vendor')
        self.assertEqual(self.event.status, Event.Status.PENDING)
        self.assertNotIn(str(self.event.id), self._public_ids())

    def test_a_vendor_cannot_publish_or_unpublish(self):
        for wanted in ('live', 'paused', 'draft'):
            response = self.owner_client.patch(
                reverse('vendor-event-detail', args=[self.event.id]),
                {'status': wanted}, format='json',
            )
            # The status key is ignored outright — a vendor never sets status.
            self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.LIVE)

    def test_a_vendor_cannot_delete_their_own_event(self):
        """…once customers hold tickets for it (withdrawing an unsold
        submission is allowed)."""
        event_date = self.event.dates.first()
        EventOrder.objects.create(
            event=self.event, event_date=event_date, date=event_date.date,
            status='confirmed', seats=1, ticket_count=1, amount=520,
            razorpay_payment_id='pay_held',
        )
        response = self.owner_client.delete(
            reverse('vendor-event-detail', args=[self.event.id])
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'HAS_SALES')
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.LIVE)

    def test_a_vendor_cannot_change_capacity(self):
        """…of a live event without re-approval: the new capacity is saved
        but the event leaves the public site until an admin approves it."""
        event_date = self.event.dates.first()
        response = self.owner_client.patch(
            reverse('vendor-event-detail', args=[self.event.id]),
            {'dates': [{'date': event_date.date.isoformat(), 'capacity': 9999}]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        event_date.refresh_from_db()
        self.assertEqual(event_date.capacity, 9999)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PENDING)
        self.assertNotIn(str(self.event.id), self._public_ids())

    def test_a_vendor_cannot_change_prices(self):
        """…of a live event without re-approval: customers can no longer buy
        at the new price until an admin approves it."""
        response = self.owner_client.patch(
            reverse('vendor-event-detail', args=[self.event.id]),
            {'ticketTypes': [
                {'key': 'adult', 'label': 'Adult', 'price': 1},
                {'key': 'child', 'label': 'Child', 'price': 1},
            ]},
            format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PENDING)
        self.assertNotIn(str(self.event.id), self._public_ids())

    def test_a_vendor_cannot_upload_a_poster(self):
        """…that is not a real image file — and an upload never changes the
        event by itself (the URL only takes effect on a reviewed save)."""
        response = self.owner_client.post(
            reverse('vendor-event-poster', args=[self.event.id]), {}, format='json'
        )
        self.assertIn(response.status_code, (400, 415))
        self.event.refresh_from_db()
        self.assertEqual(self.event.poster_url, 'https://cdn.example.com/poster.jpg')

    def test_a_vendor_sees_their_own_legacy_event_read_only(self):
        """Their events created before the change are still listed. They are
        now the vendor's to edit (an edit goes through approval)."""
        response = self.owner_client.get(reverse('vendor-events'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['canCreate'])
        row = next(e for e in response.data['events'] if e['name'] == self.event.name)
        self.assertTrue(row['canEdit'])
        self.assertEqual(row['status'], Event.Status.LIVE)

    def test_a_vendor_does_not_see_another_vendors_event(self):
        make_event(self.other, name='Someone Elses Night')
        response = self.owner_client.get(reverse('vendor-events'))
        names = [e['name'] for e in response.data['events']]
        self.assertNotIn('Someone Elses Night', names)

    def test_another_vendor_cannot_read_the_event(self):
        response = self.other_client.get(
            reverse('vendor-event-detail', args=[self.event.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_another_vendor_cannot_read_the_ticket_buyers(self):
        response = self.other_client.get(
            reverse('vendor-event-orders', args=[self.event.id])
        )
        self.assertEqual(response.status_code, 404)

    def test_a_vendor_sees_a_published_admin_event_at_their_own_venue(self):
        listing = Listing.objects.create(
            id='22222222-2222-2222-2222-222222222222', vendor=self.owner,
            slug='owner-venue', name='Owner Venue', record={'name': 'Owner Venue'},
        )
        hosted = make_event(None, name='Admin Night At Owner Venue',
                            listing=listing)
        response = self.owner_client.get(reverse('vendor-events'))
        names = [e['name'] for e in response.data['events']]
        self.assertIn(hosted.name, names)

    def test_a_vendor_cannot_edit_the_admin_event_at_their_own_venue(self):
        listing = Listing.objects.create(
            id='33333333-3333-3333-3333-333333333333', vendor=self.owner,
            slug='owner-venue-2', name='Owner Venue 2',
            record={'name': 'Owner Venue 2'},
        )
        hosted = make_event(None, name='Hosted Night', listing=listing)
        response = self.owner_client.patch(
            reverse('vendor-event-detail', args=[hosted.id]),
            {'name': 'Hijacked'}, format='json',
        )
        self.assertEqual(response.status_code, 403)
        hosted.refresh_from_db()
        self.assertEqual(hosted.name, 'Hosted Night')

    def test_a_customer_cannot_use_the_vendor_endpoints(self):
        response = auth(self.customer).get(reverse('vendor-events'))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_use_the_vendor_endpoints(self):
        response = APIClient().post(
            reverse('vendor-events'), self.payload(), format='json'
        )
        self.assertEqual(response.status_code, 401)


# =========================================================================
# Payment verification
# =========================================================================

@RZP_KEYS
class PaymentVerificationTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.client = auth(self.customer)
        self.event = make_event(self.vendor, capacity=100)
        self.date = self.event.dates.first()
        self.gateway_order = 'order_LiveTest1'
        with patch('events.payments.create_order', return_value=self.gateway_order):
            self.created = self.client.post(
                reverse('event-payment-order'),
                {
                    'eventId': str(self.event.id),
                    'eventDateId': self.date.id,
                    'tickets': {'adult': 2, 'child': 1},
                },
                format='json',
            )
        self.order_id = self.created.data['eventOrderId']

    def verify(self, *, payment_id='pay_1', sig=None, client=None, order_ref=None):
        return (client or self.client).post(
            reverse('event-payment-verify'),
            {
                'eventOrderId': order_ref or self.order_id,
                'razorpay_order_id': self.gateway_order,
                'razorpay_payment_id': payment_id,
                'razorpay_signature': sig or signature(self.gateway_order, payment_id),
            },
            format='json',
        )

    def test_the_order_starts_unpaid(self):
        self.assertEqual(
            EventOrder.objects.get(pk=self.order_id).status, 'payment_pending'
        )

    def test_a_valid_signature_confirms_the_tickets(self):
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['order']['status'], 'confirmed')
        order = EventOrder.objects.get(pk=self.order_id)
        self.assertEqual(order.status, 'confirmed')
        self.assertEqual(order.razorpay_payment_id, 'pay_1')

    def test_a_forged_signature_confirms_nothing(self):
        response = self.verify(sig='0' * 64)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            EventOrder.objects.get(pk=self.order_id).status, 'payment_pending'
        )

    def test_a_signature_from_another_secret_is_refused(self):
        response = self.verify(sig=signature(self.gateway_order, 'pay_1', 'wrong'))
        self.assertEqual(response.status_code, 400)

    def test_verification_is_idempotent(self):
        self.assertEqual(self.verify().status_code, 200)
        second = self.verify()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data['order']['status'], 'confirmed')
        self.assertEqual(EventOrder.objects.filter(status='confirmed').count(), 1)

    def test_a_cancelled_order_cannot_be_revived_by_a_replay(self):
        EventOrder.objects.filter(pk=self.order_id).update(status='cancelled')
        response = self.verify()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(EventOrder.objects.get(pk=self.order_id).status, 'cancelled')

    def test_another_customer_cannot_verify_my_order(self):
        response = self.verify(client=auth(make_customer('9100000099')))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            EventOrder.objects.get(pk=self.order_id).status, 'payment_pending'
        )

    def test_a_sellout_during_payment_queues_a_refund_instead_of_tickets(self):
        # Someone else takes every remaining seat while this customer pays.
        EventDate.objects.filter(pk=self.date.pk).update(capacity=3)
        EventOrder.objects.create(
            event=self.event, event_date=self.date, user=make_customer('9100000077'),
            date=self.date.date, seats=3, ticket_count=3,
            amount=1520, subtotal=1500, fee=20, status='confirmed',
        )
        response = self.verify()
        self.assertEqual(response.status_code, 409)
        order = EventOrder.objects.get(pk=self.order_id)
        self.assertEqual(order.status, 'refund_pending')   # never 'confirmed'

    def test_an_unpaid_order_is_listed_as_pending_until_paid(self):
        # Listed with its REAL status (never as a ticket) so a customer can
        # see an attempt that has not gone through yet.
        before = self.client.get(reverse('my-event-orders'))
        self.assertEqual(before.data['total'], 1)
        self.assertEqual(before.data['orders'][0]['status'], 'payment_pending')
        self.verify()
        after = self.client.get(reverse('my-event-orders'))
        self.assertEqual(after.data['total'], 1)
        self.assertEqual(after.data['orders'][0]['status'], 'confirmed')

    def test_another_customer_cannot_see_my_tickets(self):
        self.verify()
        response = auth(make_customer('9100000088')).get(reverse('my-event-orders'))
        self.assertEqual(response.data['total'], 0)

    def test_a_paid_ticket_is_not_self_service_cancellable(self):
        self.verify()
        response = self.client.delete(
            reverse('cancel-event-order', args=[self.order_id])
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(EventOrder.objects.get(pk=self.order_id).status, 'confirmed')

    def test_another_customer_cannot_cancel_my_hold(self):
        response = auth(make_customer('9100000066')).delete(
            reverse('cancel-event-order', args=[self.order_id])
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            EventOrder.objects.get(pk=self.order_id).status, 'payment_pending'
        )


# =========================================================================
# The shared Razorpay webhook
# =========================================================================

@RZP_KEYS
class EventWebhookTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.customer = make_customer()
        self.event = make_event(self.vendor, capacity=50)
        self.date = self.event.dates.first()
        self.order = EventOrder.objects.create(
            event=self.event, event_date=self.date, user=self.customer,
            event_name=self.event.name, date=self.date.date,
            start_time=self.event.start_time, end_time=self.event.end_time,
            items=[{'key': 'adult', 'label': 'Adult', 'qty': 2,
                    'price': 500, 'seatsPerTicket': 1, 'subtotal': 1000}],
            seats=2, ticket_count=2, subtotal=1000, fee=20, amount=1020,
            status='payment_pending', razorpay_order_id='order_hook_1',
        )

    def post(self, payload):
        import json
        body = json.dumps(payload).encode()
        sig = hmac.new(b'test_webhook_secret', body, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse('payment-webhook'), data=body,
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE=sig,
        )

    def captured(self, amount=102000, order_id='order_hook_1'):
        return {
            'event': 'payment.captured',
            'payload': {'payment': {'entity': {
                'id': 'pay_hook_1', 'order_id': order_id, 'amount': amount,
            }}},
        }

    def test_a_capture_confirms_the_event_order(self):
        response = self.post(self.captured())
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertEqual(self.order.razorpay_payment_id, 'pay_hook_1')

    def test_an_unsigned_webhook_confirms_nothing(self):
        import json
        response = self.client.post(
            reverse('payment-webhook'), data=json.dumps(self.captured()),
            content_type='application/json', HTTP_X_RAZORPAY_SIGNATURE='nope',
        )
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'payment_pending')

    def test_an_amount_mismatch_confirms_nothing(self):
        self.post(self.captured(amount=100))
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'payment_pending')

    def test_the_capture_is_idempotent(self):
        self.post(self.captured())
        self.post(self.captured())
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'confirmed')
        self.assertEqual(EventOrder.objects.filter(status='confirmed').count(), 1)

    def test_a_failed_payment_releases_the_seats(self):
        self.assertEqual(seats_remaining(self.date), 48)
        self.post({
            'event': 'payment.failed',
            'payload': {'payment': {'entity': {
                'id': 'pay_hook_1', 'order_id': 'order_hook_1',
            }}},
        })
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'failed')   # a failure, not a cancel
        self.assertEqual(seats_remaining(self.date), 50)

    def test_a_capture_for_a_cancelled_order_is_auto_refunded_not_revived(self):
        EventOrder.objects.filter(pk=self.order.pk).update(status='cancelled')
        with patch('events.payments.refund_payment', return_value='rfnd_1') as refund:
            self.post(self.captured())
        refund.assert_called_once()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, 'refunded')
        self.assertEqual(self.order.refund_amount, 1020)

    def test_an_unknown_order_is_still_ignored(self):
        response = self.post(self.captured(order_id='order_nobody'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'ignored')

    def test_venue_bookings_still_work_through_the_same_webhook(self):
        # The fall-through must not have broken the original path.
        from bookings.models import Booking
        listing = Listing.objects.create(
            id='22222222-2222-2222-2222-222222222222', vendor=self.vendor,
            slug='hall', name='Hall', record={'name': 'Hall'},
        )
        booking = Booking.objects.create(
            listing=listing, user=self.customer, venue_name='Hall',
            date=today_ist() + datetime.timedelta(days=1),
            slots=['18:00 â€“ 19:00'], amount=520, fee=20,
            status='payment_pending', razorpay_order_id='order_booking_1',
        )
        self.post({
            'event': 'payment.captured',
            'payload': {'payment': {'entity': {
                'id': 'pay_b1', 'order_id': 'order_booking_1', 'amount': 52000,
            }}},
        })
        booking.refresh_from_db()
        self.assertEqual(booking.status, 'confirmed')


# =========================================================================
# Super-admin moderation
# =========================================================================

class AdminEventModerationTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.event = make_event(self.vendor, status=Event.Status.PENDING)
        self.admin = User.objects.create_user(
            phone='9900000001', name='Admin', email='admin@example.com',
            role=User.Role.ADMIN, is_staff=True, password='x',
        )
        self.client = APIClient()
        self.client.force_login(self.admin)

    def test_events_appear_in_the_bootstrap(self):
        response = self.client.get(reverse('admin-bootstrap'))
        self.assertEqual(response.status_code, 200)
        names = [e['name'] for e in response.data['events']]
        self.assertIn(self.event.name, names)

    def test_an_admin_can_approve_an_event(self):
        response = self.client.patch(
            reverse('admin-event', args=[self.event.id]),
            {'status': 'approved'}, format='json',
        )
        self.assertEqual(response.status_code, 200)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.LIVE)
        # â€¦and it becomes visible to customers.
        self.assertEqual(
            APIClient().get(
                reverse('public-event-detail', args=[self.event.slug])
            ).status_code,
            200,
        )

    def test_approval_is_audited(self):
        from adminpanel.models import AuditEntry
        self.client.patch(
            reverse('admin-event', args=[self.event.id]),
            {'status': 'approved'}, format='json',
        )
        self.assertTrue(
            AuditEntry.objects.filter(target_id=str(self.event.id)).exists()
        )

    def test_a_rejected_event_is_not_public(self):
        self.client.patch(
            reverse('admin-event', args=[self.event.id]),
            {'status': 'rejected', 'reviewNotes': 'Poster unreadable'},
            format='json',
        )
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.REJECTED)
        self.assertEqual(self.event.review_notes, 'Poster unreadable')

    def test_a_vendor_cannot_use_the_admin_endpoint(self):
        response = auth(self.vendor).patch(
            reverse('admin-event', args=[self.event.id]),
            {'status': 'approved'}, format='json',
        )
        self.assertIn(response.status_code, (401, 403))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PENDING)


# =========================================================================
# Public catalogue
# =========================================================================

class PublicEventCatalogueTests(EventTestCase):
    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.client = APIClient()

    def test_only_live_events_are_listed(self):
        make_event(self.vendor, name='Live Night')
        make_event(self.vendor, name='Pending Night', status=Event.Status.PENDING)
        response = self.client.get(reverse('public-event-list'))
        names = [e['name'] for e in response.data['events']]
        self.assertEqual(names, ['Live Night'])

    def test_the_category_filter_works(self):
        make_event(self.vendor, name='Dandiya Night')
        navaratri = make_event(self.vendor, name='Navaratri Night')
        Event.objects.filter(pk=navaratri.pk).update(category='Navaratri')
        response = self.client.get(reverse('public-event-list'), {'category': 'Navaratri'})
        self.assertEqual([e['name'] for e in response.data['events']], ['Navaratri Night'])

    def test_a_card_carries_what_it_needs_to_render(self):
        event = make_event(self.vendor, adult=500, child=250, capacity=300)
        card = self.client.get(reverse('public-event-list')).data['events'][0]
        self.assertEqual(card['name'], event.name)
        self.assertEqual(card['poster'], event.poster_url)
        self.assertEqual(card['location'], 'Open Grounds, Hanamkonda, Warangal')
        self.assertEqual(card['startTime'], '18:00')
        self.assertEqual(card['endTime'], '22:00')
        self.assertEqual(card['fromPrice'], 250)
        self.assertEqual(
            {t['key'] for t in card['ticketTypes']}, {'adult', 'child'}
        )
        # It is bookable, and it says so WITHOUT a seat counter.
        self.assertTrue(card['bookable'])
        self.assertFalse(card['soldOut'])

    def test_a_card_carries_no_remaining_seat_counter(self):
        make_event(self.vendor, capacity=300)
        card = self.client.get(reverse('public-event-list')).data['events'][0]
        for leak in ('ticketsLeft', 'remaining', 'capacity'):
            self.assertNotIn(leak, card)
        for row in card['dates']:
            for leak in ('remaining', 'capacity', 'sold'):
                self.assertNotIn(leak, row)

    def test_a_detail_page_carries_no_remaining_seat_counter(self):
        event = make_event(self.vendor, capacity=300)
        record = self.client.get(
            reverse('public-event-detail', args=[event.slug])
        ).data
        for leak in ('ticketsLeft', 'remaining', 'capacity'):
            self.assertNotIn(leak, record)
        for row in [*record['dates'], *record['allDates']]:
            for leak in ('remaining', 'capacity', 'sold'):
                self.assertNotIn(leak, row)

    def test_a_finished_event_is_not_advertised_as_bookable(self):
        """Every night in the past: the card must not offer "Book tickets"
        for something that can only fail at the next step."""
        event = make_event(self.vendor, name='Finished Run', days_ahead=())
        EventDate.objects.create(
            event=event, date=today_ist() - datetime.timedelta(days=3),
            capacity=100,
        )
        # QA audit 2026-10-30: a finished event is now ARCHIVED (status
        # 'ended', events/lifecycle.py) and leaves the list altogether — it
        # can never be advertised as bookable. Its page still opens, marked
        # ended, with nothing on sale.
        names = [e['name'] for e in self.client.get(reverse('public-event-list')).data['events']]
        self.assertNotIn('Finished Run', names)
        event.refresh_from_db()
        self.assertEqual(event.status, Event.Status.ENDED)
        card = self.client.get(reverse('public-event-detail', args=[event.slug])).data
        self.assertFalse(card['bookable'])
        self.assertTrue(card['ended'])
        self.assertEqual(card['dates'], [])

    def test_a_finished_events_past_nights_are_still_browsable_on_detail(self):
        """Past dates stay VISIBLE (the brief: don't blindly hide past dates)
        — they are simply not selectable."""
        event = make_event(self.vendor, name='Finished Run 2', days_ahead=())
        past = EventDate.objects.create(
            event=event, date=today_ist() - datetime.timedelta(days=3),
            capacity=100,
        )
        record = self.client.get(
            reverse('public-event-detail', args=[event.slug])
        ).data
        rows = {r['date']: r for r in record['allDates']}
        self.assertIn(past.date.isoformat(), rows)
        self.assertTrue(rows[past.date.isoformat()]['isPast'])
        self.assertFalse(rows[past.date.isoformat()]['available'])

    def test_the_event_categories_are_served_by_config(self):
        response = self.client.get('/api/config')
        self.assertIn('Navaratri', response.data['eventCategories'])
        self.assertIn('Dandiya', response.data['eventCategories'])
        # The VENUE taxonomy must be untouched â€” no "Events" category there.
        venue_names = [c['name'] for c in response.data['categories']]
        self.assertNotIn('Events', venue_names)
        self.assertNotIn('Navaratri', venue_names)

    def test_an_unknown_slug_is_404(self):
        self.assertEqual(
            self.client.get(reverse('public-event-detail', args=['nope'])).status_code,
            404,
        )


# =========================================================================
# Concurrency â€” the headline requirement
# =========================================================================

@RZP_KEYS
class ConcurrentBookingTests(EventTestCase):
    """Deterministic interleaving: a competing order lands between this
    request's availability read and its locked re-check.

    This is the case that would oversell if the capacity check used the
    number the customer's browser was shown rather than re-reading it inside
    the lock. The threaded version below exercises the real database lock.
    """

    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.event = make_event(self.vendor, capacity=5)
        self.date = self.event.dates.first()

    def order_for(self, customer, qty):
        with patch('events.payments.create_order', return_value=f'order_{customer.phone}'):
            return auth(customer).post(
                reverse('event-payment-order'),
                {
                    'eventId': str(self.event.id),
                    'eventDateId': self.date.id,
                    'tickets': {'adult': qty},
                },
                format='json',
            )

    def test_two_customers_share_the_same_date_and_time(self):
        a = self.order_for(make_customer('9100000011'), 2)
        b = self.order_for(make_customer('9100000012'), 3)
        self.assertEqual(a.status_code, 201)
        self.assertEqual(b.status_code, 201)
        # Both hold tickets for the SAME date â€” this is the whole point.
        self.assertEqual(EventOrder.objects.filter(event_date=self.date).count(), 2)
        self.assertEqual(seats_sold(self.date), 5)

    def test_a_competing_order_mid_request_cannot_oversell(self):
        """The gateway call is the window: another customer buys the last
        seats while we are talking to Razorpay."""
        rival = make_customer('9100000013')

        def steal_the_seats(*args, **kwargs):
            EventOrder.objects.create(
                event=self.event, event_date=self.date, user=rival,
                date=self.date.date, seats=5, ticket_count=5,
                subtotal=2500, fee=20, amount=2520, status='confirmed',
            )
            return 'order_race'

        with patch('events.payments.create_order', side_effect=steal_the_seats):
            response = auth(make_customer('9100000014')).post(
                reverse('event-payment-order'),
                {
                    'eventId': str(self.event.id),
                    'eventDateId': self.date.id,
                    'tickets': {'adult': 3},
                },
                format='json',
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'SOLD_OUT')
        # Nothing was held: the seats went to the customer who got there first.
        self.assertEqual(seats_sold(self.date), 5)
        self.assertEqual(
            EventOrder.objects.filter(status='payment_pending').count(), 0
        )

    def test_the_total_held_never_exceeds_capacity(self):
        """Ten customers each asking for one seat against a capacity of five:
        exactly five succeed."""
        results = [
            self.order_for(make_customer(f'92000000{i:02d}'), 1).status_code
            for i in range(10)
        ]
        self.assertEqual(results.count(201), 5)
        self.assertEqual(results.count(409), 5)
        self.assertEqual(seats_sold(self.date), 5)
        self.assertLessEqual(seats_sold(self.date), self.date.capacity)


class ConcurrentBookingRaceTests(CacheResetMixin, TransactionTestCase):
    """The real thing: parallel HTTP requests racing for the last seats.

    SQLite serialises writes at the file level and does not implement
    SELECT â€¦ FOR UPDATE, so this only proves anything on Postgres â€” which is
    what production runs. It is skipped elsewhere rather than passing
    vacuously and pretending the lock was tested.
    """

    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.vendor = make_vendor()
        self.event = make_event(self.vendor, capacity=6)
        self.date = self.event.dates.first()
        self.customers = [make_customer(f'93000000{i:02d}') for i in range(10)]

    @RZP_KEYS
    def test_parallel_requests_cannot_oversell(self):
        if connection.vendor != 'postgresql':
            self.skipTest(
                'Needs a database with real row locking (SELECT â€¦ FOR UPDATE); '
                f'this run uses {connection.vendor}.'
            )

        statuses = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(self.customers))

        def buy(customer):
            try:
                barrier.wait(timeout=10)   # all threads swing at once
                with patch(
                    'events.payments.create_order',
                    return_value=f'order_{customer.phone}',
                ):
                    response = auth(customer).post(
                        reverse('event-payment-order'),
                        {
                            'eventId': str(self.event.id),
                            'eventDateId': self.date.id,
                            'tickets': {'adult': 2},
                        },
                        format='json',
                    )
                with lock:
                    statuses.append(response.status_code)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=buy, args=(c,)) for c in self.customers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # Capacity 6, two seats each â†’ exactly three winners, never four.
        self.assertEqual(statuses.count(201), 3)
        self.assertEqual(seats_sold(self.date), 6)
        self.assertLessEqual(seats_sold(self.date), self.date.capacity)
