"""
Who a campaign may reach.

ELIGIBLE (every segment starts here, no exceptions):
  * an active customer account (is_customer, is_active)
  * with an email address
  * that opted IN to promotional email themselves (EmailPreference)
  * whose address is not suppressed (hard bounce / spam complaint)

SEGMENTS narrow the eligible set:

  all           every eligible customer
  location      booked a venue in a state (and optionally a city / district)
  booked        has a confirmed venue booking (optionally within N days)
  not_booked    has never had a confirmed venue booking
  lapsed        booked before, but nothing in the last N days
  category      booked a venue of one category (Party hall, Resort, …)
  event_buyers  bought tickets to an event

WHERE "LOCATION" COMES FROM: customers' own location is kept on their device
only (the site never sends it to the server), so a customer's state / city
is the location of the venues they booked. That is the only location the
server honestly knows.
"""
import datetime
import re

from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.models import User

from .models import EmailPreference, EmailSuppression

SEGMENTS = {
    'all': 'All eligible customers',
    'location': 'Customers in a state or city',
    'booked': 'Customers who have booked a venue',
    'not_booked': 'Customers who have never booked',
    'lapsed': 'Past customers who have not booked recently',
    'category': 'Customers who booked a venue category',
    'event_buyers': 'Customers who bought event tickets',
}
# A venue booking that stands: paid / confirmed, or already happened.
BOOKED_STATUSES = ('confirmed', 'completed')
EVENT_PAID_STATUSES = ('confirmed', 'refund_pending')
MAX_DAYS = 3650
DEFAULT_LAPSED_DAYS = 90


class AudienceError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Audience definition (stored on Campaign.audience)
# ---------------------------------------------------------------------------

def clean_audience(raw):
    """Validate the admin's segment choice -> the dict stored on the campaign."""
    raw = raw if isinstance(raw, dict) else {}
    segment = str(raw.get('segment') or 'all').strip()
    if segment not in SEGMENTS:
        raise AudienceError('Choose a valid audience.')
    audience = {'segment': segment}
    if segment == 'location':
        state = str(raw.get('state') or '').strip()
        if not state:
            raise AudienceError('Choose a state.')
        from venues.regions import canonical_state
        audience['state'] = canonical_state(state) or state[:100]
        city = str(raw.get('city') or '').strip()[:100]
        if city:
            audience['city'] = city
    if segment == 'category':
        category = str(raw.get('category') or '').strip()[:50]
        if not category:
            raise AudienceError('Choose a venue category.')
        audience['category'] = category
    if segment in ('booked', 'lapsed'):
        days = raw.get('days')
        if days in (None, ''):
            days = DEFAULT_LAPSED_DAYS if segment == 'lapsed' else None
        if days is not None:
            try:
                days = int(days)
            except (TypeError, ValueError):
                raise AudienceError('Days must be a whole number.')
            if not 1 <= days <= MAX_DAYS:
                raise AudienceError(f'Days must be between 1 and {MAX_DAYS}.')
            audience['days'] = days
    return audience


def describe(audience):
    audience = audience or {}
    segment = audience.get('segment') or 'all'
    days = audience.get('days')
    if segment == 'location':
        place = ', '.join(p for p in (audience.get('city'), audience.get('state')) if p)
        return f'Customers who booked in {place}'
    if segment == 'category':
        return f'Customers who booked a {audience.get("category")}'
    if segment == 'booked' and days:
        return f'Customers who booked in the last {days} days'
    if segment == 'lapsed':
        return f'Past customers with no booking in the last {days or DEFAULT_LAPSED_DAYS} days'
    return SEGMENTS.get(segment, SEGMENTS['all'])


# ---------------------------------------------------------------------------
# Querysets
# ---------------------------------------------------------------------------

def _customers():
    return User.objects.filter(is_active=True, is_customer=True)


def _with_email(qs):
    return qs.exclude(email__isnull=True).exclude(email='')


def _suppressed_q():
    return Exists(EmailSuppression.objects.filter(email=OuterRef('email_lower')))


def eligible_customers():
    """THE consent gate. Every send path goes through this queryset."""
    return (
        _with_email(_customers())
        .filter(email_preference__marketing_opt_in=True)
        .annotate(email_lower=Lower('email'))
        .exclude(_suppressed_q())
    )


def _booking_q(**filters):
    from bookings.models import Booking
    return Booking.objects.filter(user=OuterRef('pk'), status__in=BOOKED_STATUSES, **filters)


def segment_queryset(audience):
    audience = audience or {}
    segment = audience.get('segment') or 'all'
    qs = eligible_customers()
    days = audience.get('days')
    since = timezone.now() - datetime.timedelta(days=days) if days else None

    if segment == 'all':
        return qs
    if segment == 'booked':
        return qs.filter(Exists(_booking_q(**({'created_at__gte': since} if since else {}))))
    if segment == 'not_booked':
        return qs.exclude(Exists(_booking_q()))
    if segment == 'lapsed':
        since = since or timezone.now() - datetime.timedelta(days=DEFAULT_LAPSED_DAYS)
        return qs.filter(Exists(_booking_q())).exclude(Exists(_booking_q(created_at__gte=since)))
    if segment == 'category':
        return qs.filter(Exists(_booking_q(category__iexact=audience.get('category', ''))))
    if segment == 'event_buyers':
        from events.models import EventOrder
        return qs.filter(Exists(EventOrder.objects.filter(
            user=OuterRef('pk'), status__in=EVENT_PAID_STATUSES)))
    if segment == 'location':
        listing_ids = listings_in(audience.get('state'), audience.get('city'))
        return qs.filter(Exists(_booking_q(listing_id__in=listing_ids)))
    return qs.none()


def estimate(audience):
    """Recipient count plus why other customers are left out — the panel
    shows both before anything can be sent."""
    customers = _customers()
    with_email = _with_email(customers)
    opted_in = with_email.filter(email_preference__marketing_opt_in=True)
    suppressed = opted_in.annotate(email_lower=Lower('email')).filter(_suppressed_q()).count()
    return {
        'count': segment_queryset(audience).count(),
        'eligibleTotal': eligible_customers().count(),
        'customers': customers.count(),
        'withEmail': with_email.count(),
        'optedIn': opted_in.count(),
        'suppressed': suppressed,
        'unsubscribed': EmailPreference.objects.filter(
            marketing_opt_in=False, unsubscribed_at__isnull=False).count(),
    }


def mask_email(email):
    """r***@gmail.com — enough for an admin to recognise, not to harvest."""
    local, _, domain = str(email or '').partition('@')
    if not domain:
        return '***'
    return f'{local[:1]}***@{domain}'


# ---------------------------------------------------------------------------
# Venue locations (for the location segment and its pickers)
# ---------------------------------------------------------------------------

def _place_key(text):
    text = re.sub(r'[^a-z0-9]+', ' ', str(text or '').lower())
    return re.sub(r'\s+district$', '', text.strip())


def _listing_place(listing):
    """(state, district, city) of one listing, from its record, falling back
    to the wizard draft for rows published before the record carried them."""
    from adminpanel.formatters import _district_and_city, _draft_location
    from venues.regions import canonical_state, find_district_anywhere

    record = listing.record or {}
    detail = record.get('detail') or {}
    district, city = _district_and_city(listing)
    state = str(record.get('state') or detail.get('state') or '').strip()
    if not state:
        state = str(_draft_location(listing.pk).get('state') or '').strip()
    state = canonical_state(state) or ''
    if not state and district:
        found = find_district_anywhere(district)
        state = found[0] if found else ''
    return state, district, city


def booked_places():
    """{listing_id: (state, district, city)} for every venue that has at
    least one standing booking — the only places a segment can match."""
    from bookings.models import Booking
    from venues.models import Listing

    ids = (Booking.objects.filter(status__in=BOOKED_STATUSES, listing__isnull=False)
           .values_list('listing_id', flat=True).distinct())
    return {listing.pk: _listing_place(listing) for listing in Listing.objects.filter(pk__in=ids)}


def listings_in(state, city=''):
    state_key, city_key = _place_key(state), _place_key(city)
    matches = []
    for listing_id, (l_state, l_district, l_city) in booked_places().items():
        if _place_key(l_state) != state_key:
            continue
        if city_key and city_key not in (_place_key(l_city), _place_key(l_district)):
            continue
        matches.append(listing_id)
    return matches


def location_options():
    """[{state, cities: [...]}] — the states and cities that can match."""
    by_state = {}
    for state, district, city in booked_places().values():
        if not state:
            continue
        cities = by_state.setdefault(state, set())
        for name in (city, district):
            if name:
                cities.add(name)
    return [{'state': s, 'cities': sorted(c, key=str.lower)} for s, c in sorted(by_state.items())]


def category_options():
    from bookings.models import Booking
    names = (Booking.objects.filter(status__in=BOOKED_STATUSES).exclude(category='')
             .values_list('category', flat=True).distinct())
    return sorted(set(names), key=str.lower)
