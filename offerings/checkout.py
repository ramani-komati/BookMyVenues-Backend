"""
Pricing and booking for memberships, resort packages and big screen
surprises — shared by the quote endpoint and the Razorpay order endpoint, so
the price a customer is shown is exactly the price the order charges.

Every figure is computed here from the venue's own rows; the client's
`amount` is only CHECKED (a mismatch is refused with the server total, like
bookings.views does for slots), never trusted. The platform booking fee is
the same Settings fee slot bookings pay.

validate(body, user) -> (Checkout, None) | (None, Response)
"""
import calendar
import datetime
from dataclasses import dataclass, field
from typing import Callable

from rest_framework import status
from rest_framework.response import Response

from bookings.slots import parse_date, today_ist
from venues.models import Listing

from . import availability
from .models import FitnessPlan, Membership, OfferingProfile, PackageBooking, ResortPackage, SurpriseBooking, SurpriseShow
from .schema import minutes

KINDS = {'fitness': 'membership', 'resort': 'package', 'surprise': 'surprise'}


class CheckoutError(Exception):
    def __init__(self, message, http_status=status.HTTP_400_BAD_REQUEST, code=''):
        super().__init__(message)
        self.message, self.http_status, self.code = message, http_status, code


@dataclass
class Checkout:
    listing: Listing
    kind: str                       # Booking.kind: membership | package | surprise
    date: datetime.date
    slots: list
    lines: list                     # [{name, qty, price}] — what the money buys
    fee: int
    amount: int
    unit_label: str = ''
    occasion: str = ''
    summary: dict = field(default_factory=dict)
    # Re-run under the venue lock right before the booking is written.
    recheck: Callable[[], None] = lambda: None
    # Writes the kind's own row for the new booking.
    create_detail: Callable[[object], None] = lambda booking: None


def _fee():
    from bookings.views import _booking_fee
    return _booking_fee()


def _int(value, label, minimum=0, maximum=1000):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise CheckoutError(f'{label} must be a number.') from None
    if not (minimum <= number <= maximum):
        raise CheckoutError(f'{label} must be between {minimum} and {maximum}.')
    return number


def _text(value, label, limit, required=False):
    text = '' if value is None else str(value).strip()
    if required and not text:
        raise CheckoutError(f'Enter the {label}.')
    if len(text) > limit:
        raise CheckoutError(f'The {label} must be {limit} characters or fewer.')
    return text


def _listing(body, kind):
    venue_id = str(body.get('venueId') or body.get('listingId') or '').strip()
    try:
        listing = Listing.objects.filter(pk=venue_id, status=Listing.Status.LIVE).first()
    except Exception:            # not a UUID
        listing = None
    profile = OfferingProfile.objects.filter(listing=listing, kind=kind).first() if listing else None
    if listing is None or profile is None:
        raise CheckoutError('Venue not found.', status.HTTP_404_NOT_FOUND)
    from bookings.views import _vendor_paused
    if _vendor_paused(listing):
        raise CheckoutError('This venue is temporarily not accepting bookings', status.HTTP_409_CONFLICT)
    return listing, profile.settings or {}


def _row(model, listing, key, what):
    row = model.objects.filter(listing=listing, key=str(key or '').strip()).first()
    if row is None or not row.on_sale:
        raise CheckoutError(f'That {what} is no longer available.', status.HTTP_404_NOT_FOUND)
    return row


# --- memberships --------------------------------------------------------------

def add_duration(start, value, unit):
    """The LAST day of a membership starting on `start`: 1 month from 15 Jan
    runs to 14 Feb; 1 day is the start day itself."""
    if unit == 'day':
        return start + datetime.timedelta(days=value - 1)
    if unit == 'week':
        return start + datetime.timedelta(days=7 * value - 1)
    month = start.month - 1 + value
    year = start.year + month // 12
    month = month % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day) - datetime.timedelta(days=1)


def plan_public(plan):
    return {
        'id': plan.key, 'kind': plan.kind, 'name': plan.name, 'description': plan.description,
        'price': plan.price, 'durationValue': plan.duration_value, 'durationUnit': plan.duration_unit,
        'startRule': plan.start_rule, 'startWithinDays': plan.start_within_days,
        'expiryRule': plan.expiry_rule, 'visits': plan.visits, 'facilities': plan.facilities,
        'trialAvailable': plan.trial_available, 'trialPrice': plan.trial_price,
        'joiningFee': plan.joining_fee,
    }


def is_returning_member(user, listing):
    """Has this customer held a PAID membership at this gym before? Then the
    joining fee is not charged again."""
    if user is None or not getattr(user, 'id', None):
        return False
    return Membership.objects.filter(
        user=user, listing=listing,
        booking__status__in=('confirmed', 'refund_pending'),
    ).exists()


def _membership(body, user):
    listing, settings = _listing(body, 'fitness')
    plan = _row(FitnessPlan, listing, body.get('planId'), 'membership plan')
    today = today_ist()
    if plan.start_rule == 'chosen' and body.get('startDate'):
        try:
            starts = parse_date(body.get('startDate'))
        except Exception:
            raise CheckoutError('Pick a valid start date.') from None
        if starts < today:
            raise CheckoutError('The start date has passed.')
        if (starts - today).days > plan.start_within_days:
            raise CheckoutError(f'This plan must start within {plan.start_within_days} days of buying it.')
    else:
        starts = today
    ends = add_duration(starts, plan.duration_value, plan.duration_unit)
    joining = 0 if is_returning_member(user, listing) else plan.joining_fee
    lines = [{'name': plan.name, 'qty': 1, 'price': plan.price}]
    if joining:
        lines.append({'name': 'Joining fee', 'qty': 1, 'price': joining})
    fee = _fee()
    amount = plan.price + joining + fee
    snapshot = plan_public(plan)

    def create(booking):
        Membership.objects.create(
            booking=booking, plan=plan, listing=listing, user=user, plan_snapshot=snapshot,
            starts_on=starts, ends_on=ends, visits_allowed=plan.visits, joining_fee=joining,
        )

    return Checkout(
        listing=listing, kind='membership', date=starts, slots=[], lines=lines, fee=fee, amount=amount,
        unit_label=plan.name,
        summary={'plan': snapshot, 'startsOn': starts.isoformat(), 'endsOn': ends.isoformat(),
                 'joiningFee': joining, 'visits': plan.visits},
        create_detail=create,
    )


# --- resort packages ----------------------------------------------------------

def package_public(package):
    return {
        'id': package.key, 'name': package.name, 'description': package.description,
        'kind': package.kind, 'duration': package.duration, 'pricing': package.pricing,
        'price': package.price, 'childPrice': package.child_price,
        'minPersons': package.min_persons, 'maxPersons': package.max_persons,
        'capacity': package.capacity, 'slots': package.slots,
        'checkIn': package.check_in, 'checkOut': package.check_out,
        'availableFrom': package.available_from.isoformat() if package.available_from else '',
        'availableTo': package.available_to.isoformat() if package.available_to else '',
        'inclusions': package.inclusions,
    }


def package_price(package, adults, children, start, end):
    """(base, lines) for a package booking."""
    child_rate = package.price if package.child_price is None else package.child_price
    if package.pricing == 'per_package':
        return package.price, [{'name': package.name, 'qty': 1, 'price': package.price}]
    hours = 1
    if package.pricing == 'per_hour':
        span = end - start
        if span % 60:
            raise CheckoutError('Per-hour packages need slots in whole hours.')
        hours = span // 60
    lines = [{'name': f'{package.name} — adult{" × " + str(hours) + " hr" if hours > 1 else ""}', 'qty': adults, 'price': package.price * hours}]
    if children:
        lines.append({'name': f'{package.name} — child{" × " + str(hours) + " hr" if hours > 1 else ""}', 'qty': children, 'price': child_rate * hours})
    return sum(line['qty'] * line['price'] for line in lines), lines


def _package(body, user):
    listing, settings = _listing(body, 'resort')
    package = _row(ResortPackage, listing, body.get('packageId'), 'package')
    try:
        date = parse_date(body.get('date'))
    except Exception:
        raise CheckoutError('Pick a valid date.') from None
    adults = _int(body.get('adults', 1), 'Adults', minimum=1)
    children = _int(body.get('children', 0) or 0, 'Children', minimum=0)
    persons = adults + children
    if persons < package.min_persons:
        raise CheckoutError(f'This package is for at least {package.min_persons} persons.')
    if persons > package.max_persons:
        raise CheckoutError(f'This package is for at most {package.max_persons} persons.')

    slots = availability.package_slots(package)
    wanted = str(body.get('slot') or '').strip()
    chosen = None
    for start, end in slots:
        if wanted in (f'{availability.clock(start)}-{availability.clock(end)}', availability.clock(start)) or (not wanted and len(slots) == 1):
            chosen = (start, end)
            break
    if chosen is None:
        raise CheckoutError('Pick one of the package’s time slots.')
    start, end = chosen

    for row in availability.package_slot_rows(package, settings, date):
        if row['start'] == availability.clock(start) and row['end'] == availability.clock(end) and not row['available']:
            raise CheckoutError(row['reason'] or 'That slot is not available.', status.HTTP_409_CONFLICT)

    base, lines = package_price(package, adults, children, start, end)
    fee = _fee()
    snapshot = package_public(package)
    slot_text = f'{availability.clock(start)} – {availability.clock(end)}'

    def recheck():
        if package.capacity is None:
            return
        taken = availability.package_guests(package, date, start, end)
        if taken + persons > package.capacity:
            left = max(0, package.capacity - taken)
            booked = availability.package_guests(package, date, start, end, booked_only=True)
            if booked + persons <= package.capacity:
                # Only other customers' 5-minute checkout holds are in the way.
                raise CheckoutError(
                    'This slot is temporarily reserved. Please select another slot '
                    'or try again in a few minutes.',
                    status.HTTP_409_CONFLICT,
                )
            raise CheckoutError(
                f'Only {left} place{"s" if left != 1 else ""} left for this slot — please change the guests or the slot.',
                status.HTTP_409_CONFLICT,
            )

    def create(booking):
        PackageBooking.objects.create(
            booking=booking, package=package, package_snapshot=snapshot,
            adults=adults, children=children, start_minute=start, end_minute=end,
        )

    return Checkout(
        listing=listing, kind='package', date=date, slots=[slot_text], lines=lines, fee=fee,
        amount=base + fee, unit_label=package.name,
        summary={'package': snapshot, 'adults': adults, 'children': children, 'slot': slot_text},
        recheck=recheck, create_detail=create,
    )


# --- big screen surprise ------------------------------------------------------

def show_public(show):
    return {
        'id': show.key, 'movie': show.movie, 'language': show.language, 'screen': show.screen,
        'times': show.times,
        'availableFrom': show.available_from.isoformat() if show.available_from else '',
        'availableTo': show.available_to.isoformat() if show.available_to else '',
    }


def surprise_price(settings, occasion):
    custom = (settings.get('occasionPrices') or {}).get(occasion)
    price = custom if custom else settings.get('price')
    if not price:
        raise CheckoutError('This theatre has not set a surprise price yet.', status.HTTP_409_CONFLICT)
    return price


def _media_url(value, user, what):
    """A photo / video must be one THIS customer uploaded through
    /api/offerings/surprise-media — not any URL off the internet."""
    url = str(value or '').strip()
    if not url:
        return ''
    from venues.storage import public_url
    if not url.startswith(public_url(f'surprises/{user.id}/')):
        raise CheckoutError(f'Upload the {what} again — it was not uploaded through this booking.')
    return url[:500]


def _surprise(body, user):
    listing, settings = _listing(body, 'surprise')
    show = _row(SurpriseShow, listing, body.get('showId'), 'show')
    try:
        date = parse_date(body.get('date'))
    except Exception:
        raise CheckoutError('Pick a valid date.') from None
    time = str(body.get('time') or '').strip()
    if time not in (show.times or []):
        raise CheckoutError('Pick one of the show’s timings.')
    minute = minutes(time)

    occasions = settings.get('occasions') or []
    occasion = _text(body.get('occasion'), 'occasion', 40, required=True)
    if occasions and occasion not in occasions:
        raise CheckoutError('Pick one of the occasions this theatre offers.')
    recipient = _text(body.get('recipientName'), 'recipient’s name', 60, required=True)
    message = _text(body.get('message'), 'message for the screen', 200, required=True)
    sender = _text(body.get('senderName'), 'sender name', 60)
    photo = _media_url(body.get('photoUrl'), user, 'photo')
    video = _media_url(body.get('videoUrl'), user, 'video')
    if photo and not settings.get('allowPhoto'):
        raise CheckoutError('This theatre does not accept photos.')
    if video and not settings.get('allowVideo'):
        raise CheckoutError('This theatre does not accept videos.')

    for row in availability.show_time_rows(listing, settings, show, date):
        if row['time'] == time and not row['available']:
            raise CheckoutError(row['reason'] or 'That show is not available.', status.HTTP_409_CONFLICT)

    price = surprise_price(settings, occasion)
    fee = _fee()
    lines = [{'name': f'Big screen surprise — {occasion}', 'qty': 1, 'price': price}]
    end = min(minute + 15, 24 * 60)
    slot_text = f'{availability.clock(minute)} – {availability.clock(end)}'

    def recheck():
        per_show = settings.get('maxBookingsPerShow') or 1
        if availability.screen_bookings(listing, date, show.screen, minute) >= per_show:
            raise CheckoutError('That show was just booked — please pick another timing.', status.HTTP_409_CONFLICT)
        per_day = settings.get('showsPerDay')
        if per_day is not None and availability.day_bookings(listing, date) >= per_day:
            raise CheckoutError('All of this day’s surprise slots were just booked.', status.HTTP_409_CONFLICT)

    def create(booking):
        SurpriseBooking.objects.create(
            booking=booking, show=show, movie=show.movie, screen=show.screen, show_minute=minute,
            occasion=occasion, recipient_name=recipient, message=message, sender_name=sender,
            photo_url=photo, video_url=video,
        )

    return Checkout(
        listing=listing, kind='surprise', date=date, slots=[slot_text], lines=lines, fee=fee,
        amount=price + fee, occasion=occasion,
        unit_label=f'Screen {show.screen} · {time} · {show.movie}'[:150],
        summary={'show': show_public(show), 'time': time, 'occasion': occasion,
                 'recipientName': recipient, 'message': message, 'senderName': sender,
                 'photoUrl': photo, 'videoUrl': video},
        recheck=recheck, create_detail=create,
    )


_BUILDERS = {'fitness': _membership, 'resort': _package, 'surprise': _surprise}


def validate(body, user):
    body = body if isinstance(body, dict) else {}
    kind = str(body.get('kind') or '').strip()
    builder = _BUILDERS.get(kind)
    try:
        if builder is None:
            raise CheckoutError('kind must be fitness, resort or surprise.')
        checkout = builder(body, user)
        client_amount = body.get('amount')
        if client_amount not in (None, '') and str(client_amount).strip() != str(checkout.amount):
            return None, Response(
                {'message': f'Amount mismatch: expected ₹{checkout.amount}.', 'code': 'AMOUNT_MISMATCH',
                 'expectedAmount': checkout.amount},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except CheckoutError as error:
        payload = {'message': error.message}
        if error.code:
            payload['code'] = error.code
        return None, Response(payload, status=error.http_status)
    return checkout, None


def quote_payload(checkout):
    return {
        'kind': checkout.kind,
        'date': checkout.date.isoformat(),
        'slots': checkout.slots,
        'lines': checkout.lines,
        'fee': checkout.fee,
        'amount': checkout.amount,
        **checkout.summary,
    }
