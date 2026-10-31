"""
Who a campaign may reach.

ELIGIBLE (every segment starts here, no exceptions):
  * an active customer account (is_active, and is_customer OR the PUBLIC
    role — the same rule as the admin Users list; QA audit 2026-10-30 found
    PUBLIC-role customers silently missing, so counts read 0)
  * with a VALID email address — one recipient per address (case-insensitive)
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
  active        any standing venue booking or paid event ticket (optionally
                within N days) — "relevant previous activity"
  interest_category
                INTERESTED in one of the chosen venue categories: booked one,
                or saved a venue of that category to favourites
  interest_city INTERESTED in one of the chosen cities: booked a venue there,
                saved a venue there, or bought tickets to an event there

INTERESTS are only what the customer's own activity shows (bookings,
favourites, event tickets). Nothing is inferred beyond that; a customer
without any is shown as "No recorded interests".

ONE LIST: recipients(audience) is the single source of truth — the count the
panel shows, the recipient list it pages through, and the list a send freezes
all come from it, so they cannot disagree.
WHERE "LOCATION" COMES FROM: customers' own location is kept on their device
only (the site never sends it to the server), so a customer's state / city
is the location of the venues they booked. That is the only location the
server honestly knows.
"""
import datetime
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
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
    'active': 'Customers with any booking or event activity',
    'interest_category': 'Customers interested in venue categories',
    'interest_city': 'Customers interested in cities',
}
MAX_CHOICES = 20
NO_INTERESTS = 'No recorded interests'
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
    if segment in ('interest_category', 'interest_city'):
        raw_list = raw.get('categories' if segment == 'interest_category' else 'cities')
        values = raw_list if isinstance(raw_list, list) else [raw_list] if raw_list else []
        cleaned = []
        for value in values:
            text = str(value or '').strip()[:100]
            if text and text.lower() not in {c.lower() for c in cleaned}:
                cleaned.append(text)
        if not cleaned:
            raise AudienceError(
                'Choose at least one category.' if segment == 'interest_category'
                else 'Choose at least one city.')
        if len(cleaned) > MAX_CHOICES:
            raise AudienceError(f'Choose at most {MAX_CHOICES}.')
        audience['categories' if segment == 'interest_category' else 'cities'] = cleaned
    if segment in ('booked', 'lapsed', 'active'):
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
    if segment == 'active' and days:
        return f'Customers with a booking or ticket in the last {days} days'
    if segment == 'interest_category':
        return 'Customers interested in ' + ', '.join(audience.get('categories') or [])
    if segment == 'interest_city':
        return 'Customers interested in ' + ', '.join(audience.get('cities') or [])
    return SEGMENTS.get(segment, SEGMENTS['all'])


# ---------------------------------------------------------------------------
# Querysets
# ---------------------------------------------------------------------------

def _customers():
    """Customer accounts — the admin Users list's rule (adminpanel/formatters)."""
    return User.objects.filter(is_active=True).filter(
        Q(is_customer=True) | Q(role=User.Role.PUBLIC))


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
    if segment == 'active':
        from events.models import EventOrder
        booking = _booking_q(**({'created_at__gte': since} if since else {}))
        tickets = EventOrder.objects.filter(
            user=OuterRef('pk'), status__in=EVENT_PAID_STATUSES,
            **({'created_at__gte': since} if since else {}))
        return qs.filter(Q(Exists(booking)) | Q(Exists(tickets)))
    if segment == 'interest_category':
        wanted = {_place_key(c) for c in audience.get('categories') or []}
        ids = [uid for uid, interest in interest_map().items()
               if wanted & {_place_key(c) for c in interest['categories']}]
        return qs.filter(pk__in=ids)
    if segment == 'interest_city':
        wanted = {_place_key(c) for c in audience.get('cities') or []}
        ids = [uid for uid, interest in interest_map().items()
               if wanted & {_place_key(c) for c in interest['cities']}]
        return qs.filter(pk__in=ids)
    return qs.none()


def _valid_email(email):
    try:
        validate_email(email)
    except ValidationError:
        return False
    return True


def recipients(audience):
    """THE recipient list: [(user_id, email_lower, name)] in a stable order,
    one per address, valid addresses only. Count, list and send all use it."""
    out, seen = [], set()
    rows = segment_queryset(audience).order_by('pk').values_list('pk', 'email_lower', 'name')
    for user_id, email, name in rows.iterator(chunk_size=2000):
        email = str(email or '').strip()
        if not email or email in seen or not _valid_email(email):
            continue
        seen.add(email)
        out.append((user_id, email, name or ''))
    return out


# ---------------------------------------------------------------------------
# Customer interests (bookings, favourites, event tickets — nothing invented)
# ---------------------------------------------------------------------------

def _canonical(category):
    from venues.taxonomy import canonical_category
    return canonical_category(category) or str(category or '').strip()


def interest_map(user_ids=None):
    """{user_id: {categories, cities, states, bookings, tickets, favorites,
    lastActivity, eventNames}} from the customer's own records only."""
    from bookings.models import Booking
    from events.models import EventOrder
    from venues.models import Favorite, Listing

    data = {}

    def entry(uid):
        return data.setdefault(uid, {
            'categories': set(), 'cities': set(), 'states': set(),
            'bookings': 0, 'tickets': 0, 'favorites': 0,
            'lastActivity': None, 'eventNames': [], 'place': None, 'placeAt': None,
            'sources': set(),
        })

    places = {}

    def place_of(listing_id):
        if listing_id not in places:
            listing = Listing.objects.filter(pk=listing_id).first()
            places[listing_id] = _listing_place(listing) if listing else ('', '', '')
        return places[listing_id]

    def note_place(row, state, city, when):
        if city:
            row['cities'].add(city)
        if state:
            row['states'].add(state)
        if (city or state) and (row['placeAt'] is None or (when and when > row['placeAt'])):
            row['place'], row['placeAt'] = (city, state), when

    bookings = Booking.objects.filter(status__in=BOOKED_STATUSES, user__isnull=False)
    if user_ids is not None:
        bookings = bookings.filter(user_id__in=user_ids)
    for uid, category, listing_id, created in bookings.values_list(
            'user_id', 'category', 'listing_id', 'created_at'):
        row = entry(uid)
        row['bookings'] += 1
        row['sources'].add('booking')
        if category:
            row['categories'].add(_canonical(category))
        if listing_id:
            state, district, city = place_of(listing_id)
            note_place(row, state, city or district, created)
        if created and (row['lastActivity'] is None or created > row['lastActivity']):
            row['lastActivity'] = created

    orders = EventOrder.objects.filter(status__in=EVENT_PAID_STATUSES, user__isnull=False)
    if user_ids is not None:
        orders = orders.filter(user_id__in=user_ids)
    for uid, name, category, city, created in orders.values_list(
            'user_id', 'event_name', 'event_category', 'event__city', 'created_at'):
        row = entry(uid)
        row['tickets'] += 1
        row['sources'].add('event')
        if name and name not in row['eventNames']:
            row['eventNames'].append(name)
        if city:
            note_place(row, '', city, created)
        if created and (row['lastActivity'] is None or created > row['lastActivity']):
            row['lastActivity'] = created

    favorites = Favorite.objects.all()
    if user_ids is not None:
        favorites = favorites.filter(user_id__in=user_ids)
    for uid, listing_id, category in favorites.values_list('user_id', 'listing_id', 'listing__category'):
        row = entry(uid)
        row['favorites'] += 1
        row['sources'].add('favorite')
        listing = Listing.objects.filter(pk=listing_id).only('record', 'category').first()
        record_category = ((listing.record or {}).get('category') if listing else '') or category
        if record_category:
            row['categories'].add(_canonical(record_category))
        state, district, city = place_of(listing_id)
        if city or district:
            row['cities'].add(city or district)
        if state:
            row['states'].add(state)
    return data


def interest_options():
    """Categories and cities that at least one customer is interested in."""
    categories, cities = set(), set()
    for interest in interest_map().values():
        categories |= interest['categories']
        cities |= interest['cities']
    return {
        'categories': sorted(categories, key=str.lower),
        'cities': sorted(cities, key=str.lower),
    }


def _reason(audience, interest):
    segment = (audience or {}).get('segment') or 'all'
    if segment == 'location':
        place = ', '.join(p for p in (audience.get('city'), audience.get('state')) if p)
        return f'Booked a venue in {place}'
    if segment == 'booked':
        n = interest['bookings']
        return f'{n} confirmed booking{"s" if n != 1 else ""}' + (
            f' (recent: within {audience["days"]} days)' if audience.get('days') else '')
    if segment == 'not_booked':
        return 'Has never booked a venue'
    if segment == 'lapsed':
        last = interest['lastActivity']
        return f'No booking in {audience.get("days") or DEFAULT_LAPSED_DAYS} days' + (
            f' (last activity {last.date().isoformat()})' if last else '')
    if segment == 'category':
        return f'Booked a {audience.get("category")}'
    if segment == 'event_buyers':
        names = ', '.join(interest['eventNames'][:2])
        return 'Bought event tickets' + (f': {names}' if names else '')
    if segment == 'active':
        return f'{interest["bookings"]} booking(s), {interest["tickets"]} ticket order(s)'
    if segment == 'interest_category':
        wanted = {_place_key(c) for c in audience.get('categories') or []}
        hits = [c for c in sorted(interest['categories']) if _place_key(c) in wanted]
        return 'Interested in ' + ', '.join(hits) + _via(interest)
    if segment == 'interest_city':
        wanted = {_place_key(c) for c in audience.get('cities') or []}
        hits = [c for c in sorted(interest['cities']) if _place_key(c) in wanted]
        return 'Interested in ' + ', '.join(hits) + _via(interest)
    return 'Opted in to promotional emails'


def _via(interest):
    labels = {'booking': 'bookings', 'favorite': 'favourites', 'event': 'event tickets'}
    found = [labels[k] for k in ('booking', 'favorite', 'event') if k in interest['sources']]
    return f' (from {", ".join(found)})' if found else ''


def recipient_page(audience, *, page=1, page_size=25, search=''):
    """One page of the audience for the admin panel, with each customer's
    location, recorded interests and why they qualify. Admin-only (the view
    checks IsAdmin); the list is the SAME one a send would freeze."""
    rows = recipients(audience)
    total = len(rows)
    needle = str(search or '').strip().lower()
    if needle:
        rows = [r for r in rows if needle in r[1] or needle in (r[2] or '').lower()]
    matched = len(rows)
    page = max(1, int(page or 1))
    chunk = rows[(page - 1) * page_size: page * page_size]
    interests = interest_map([uid for uid, _e, _n in chunk])
    empty = {'categories': set(), 'cities': set(), 'states': set(), 'bookings': 0,
             'tickets': 0, 'favorites': 0, 'lastActivity': None, 'eventNames': [],
             'place': None, 'placeAt': None, 'sources': set()}
    out = []
    for uid, email, name in chunk:
        interest = interests.get(uid, empty)
        city, state = interest['place'] or ('', '')
        cats = sorted(interest['categories'], key=str.lower)
        out.append({
            'id': uid,
            'name': name,
            'email': email,
            'city': city or '',
            'state': state or '',
            'interests': cats or [NO_INTERESTS],
            'hasInterests': bool(cats or interest['cities']),
            'bookings': interest['bookings'],
            'tickets': interest['tickets'],
            'favorites': interest['favorites'],
            'reason': _reason(audience, interest),
        })
    return {'total': total, 'matched': matched, 'page': page, 'pageSize': page_size,
            'recipients': out}


def estimate(audience):
    """Recipient count plus why other customers are left out — the panel
    shows both before anything can be sent. `count` is len(recipients()) —
    exactly what a send would freeze."""
    customers = _customers()
    with_email = _with_email(customers)
    opted_in = with_email.filter(email_preference__marketing_opt_in=True)
    suppressed = opted_in.annotate(email_lower=Lower('email')).filter(_suppressed_q()).count()
    segment_rows = segment_queryset(audience).count()
    count = len(recipients(audience))
    eligible_rows = list(eligible_customers().values_list('email_lower', flat=True))
    valid = [e for e in eligible_rows if e and _valid_email(e)]
    return {
        'count': count,
        'eligibleTotal': len(set(valid)),
        'invalidEmail': len(eligible_rows) - len(valid),
        'duplicates': len(valid) - len(set(valid)),
        'segmentAccounts': segment_rows,
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
