"""
Booking endpoints (contract 1.3, 2.3, 2.4, 2.5).

The critical rule: two simultaneous requests for overlapping slots —
exactly ONE wins, the other gets 409. Enforced by locking the venue's
Listing row (select_for_update) inside a transaction, which forces
concurrent bookings for the same venue to run one after another.
Error shape everywhere: {"message": "..."}.
"""
import datetime
from collections import defaultdict

from django.db import transaction
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from venues.models import Listing

from .models import Booking
from .slots import (
    SlotError,
    is_weekend,
    now_minutes_ist,
    overlaps,
    parse_date,
    parse_slots,
    slots_end_minute,
    today_ist,
    total_minutes,
)

BOOKING_FEE = 20  # default flat ₹20 per booking (contract cross-cutting rule 5)
MAX_LIMIT = 50
DEFAULT_LIMIT = 20


def _booking_fee():
    """The fee that applies TODAY — the same Settings.effective_fee() that
    GET /api/config serves, so bill and recompute always agree. Honours
    feeDate (a future date means the new fee hasn't kicked in yet)."""
    try:
        from adminpanel.models import Settings
        return Settings.load().effective_fee()
    except Exception:
        return BOOKING_FEE


def _message(text, http_status):
    return Response({'message': text}, status=http_status)


def _amount_mismatch(expected):
    """Structured 400 for a wrong amount (P5a) — the frontend reads `code`
    and `expectedAmount` directly instead of regexing the message text."""
    return Response(
        {
            'message': f'Amount mismatch: expected ₹{expected}.',
            'code': 'AMOUNT_MISMATCH',
            'expectedAmount': expected,
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _to_int(value, field):
    """Numeric form fields may arrive as strings ('120') — coerce."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        raise SlotError(f'{field} must be a number.')


def _vendor_paused(listing):
    """True when the VENDOR paused this venue (rain/maintenance) via the
    listing record's paused flag (top-level or detail.paused). Distinct from
    the admin's status='paused', which hides the venue entirely — a
    vendor-paused venue stays visible (customers see the reason) but takes no
    customer bookings. Walk-ins stay allowed (pausing blocks customers, not
    the vendor's own entries)."""
    record = listing.record or {}
    detail = record.get('detail') or {}
    return bool(record.get('paused') or detail.get('paused'))


PENDING_HOLD_MINUTES = 30  # how long an unpaid Razorpay hold keeps the slot


def exclude_dead(queryset):
    """Drop rows that are not real bookings any more — cancelled (incl.
    abandoned checkouts) and refunded. Shared by availability and the vendor
    dashboard so the slot calendar and the vendor's numbers always agree."""
    return queryset.exclude(status__in=Booking.DEAD_STATUSES)


def _active_bookings(listing, date):
    """The rows that HOLD time: everything except dead rows and EXPIRED
    payment holds (a fresh hold still blocks the slot; an abandoned one frees
    it after PENDING_HOLD_MINUTES). Availability and the overlap check both
    read exactly this queryset so they can never disagree."""
    from django.utils import timezone
    cutoff = timezone.now() - datetime.timedelta(minutes=PENDING_HOLD_MINUTES)
    return exclude_dead(
        Booking.objects.filter(listing=listing, date=date)
    ).exclude(status='payment_pending', created_at__lt=cutoff)


def _booked_intervals(listing, date, sport=None, unit=None):
    """
    (start, end) minute-intervals already booked for venue+date that CONFLICT
    with a booking for (sport, unit).

    - unit=None (whole venue / single-unit) conflicts with every booking —
      the original behaviour.
    - unit set: conflicts only with the SAME (sport, unit), plus legacy /
      whole-venue bookings (unit is None) which block every unit (safe default).
    """
    intervals = []
    for booking in _active_bookings(listing, date):
        if unit is not None and booking.unit is not None:
            if booking.unit != unit or (booking.sport or '') != (sport or ''):
                continue  # a different pitch/court/screen — no conflict
        try:
            intervals.extend(parse_slots(booking.slots))
        except SlotError:
            continue  # never let one bad historic row break new bookings
    return intervals


def _parse_unit(body):
    """
    (sport, unit, unitLabel) from a booking request.
    unit is a 1-based int, or None for a whole-venue / single-unit booking.
    """
    sport = str(body.get('sport') or '').strip()
    raw = body.get('unit')
    if raw in (None, ''):
        unit = None
    else:
        unit = _to_int(raw, 'unit')
        if unit < 1:
            raise SlotError('unit must be 1 or higher.')
    unit_label = str(body.get('unitLabel') or '').strip()
    return sport, unit, unit_label


def _price_at(values, unit):
    """values[unit-1] as an int, or None when absent/blank."""
    values = values or []
    if unit is None or not (1 <= unit <= len(values)):
        return None
    raw = str(values[unit - 1]).strip()
    return _to_int(raw, 'unit price') if raw else None


# The wizard writes `unitWeekendPrices`; `weekendUnitPrices` is accepted as an
# alias so either spelling works and no vendor data needs rewriting.
WEEKEND_UNIT_KEYS = ('unitWeekendPrices', 'weekendUnitPrices')


def _weekend_price_at(source, unit):
    """The weekend per-unit price under either accepted key name."""
    for key in WEEKEND_UNIT_KEYS:
        rate = _price_at(source.get(key), unit)
        if rate is not None:
            return rate
    return None


def _amount_or_none(value, field):
    text = str(value or '').strip()
    return _to_int(text, field) if text else None


def base_rate(listing, weekend=False):
    """
    The venue's hourly rate for a whole-venue booking.

    Weekend (Sat/Sun IST) uses `weekendPrice` when the vendor has set one —
    top level or under detail — otherwise the ordinary `price`. A venue with
    no weekend price simply keeps its weekday rate, so nothing breaks.
    """
    record = listing.record or {}
    if weekend:
        for source in (record.get('weekendPrice'),
                       (record.get('detail') or {}).get('weekendPrice')):
            rate = _amount_or_none(source, 'weekend price')
            if rate is not None:
                return rate
    return _to_int(record.get('price') or 0, 'venue price')


def _unit_rate(listing, sport, unit, weekend=False):
    """
    The listing's hourly rate for a specific (sport, unit), or None when the
    venue isn't priced per unit (caller then falls back to base_rate).

      playzone:     detail.sports[name==sport].unitPrices[unit-1]
      hall/theatre: detail.unitPrices[unit-1]

    On a weekend the matching weekend list is preferred —
    `weekendUnitPrices[unit-1]`, then the sport's `weekendPrice` — falling
    back to the weekday value whenever the vendor hasn't set one.
    """
    if unit is None:
        return None
    detail = listing.record.get('detail') or {}
    if sport:
        for entry in detail.get('sports') or []:
            if str(entry.get('name')) != sport:
                continue
            if weekend:
                rate = _weekend_price_at(entry, unit)
                if rate is not None:
                    return rate
                rate = _amount_or_none(entry.get('weekendPrice'), 'sport weekend price')
                if rate is not None:
                    return rate
            rate = _price_at(entry.get('unitPrices'), unit)
            if rate is not None:
                return rate
            return _amount_or_none(entry.get('price'), 'sport price')
        return None
    if weekend:
        rate = _weekend_price_at(detail, unit)
        if rate is not None:
            return rate
    return _price_at(detail.get('unitPrices'), unit)


def _addon_total(listing, requested_addons):
    """
    (total, cleaned_lines) for the add-on line items. With LIVE payments the
    client's prices can no longer be trusted — every line is priced from the
    venue's stored data instead:
      - plain add-ons        -> detail.addons        (name match)
      - 'Package — <label>'  -> detail.packages      (label match)
      - 'Extra person(s)'    -> detail.extraPersonPrice (qty ≤ maxExtraPersons)
    Matching is case-insensitive. Unknown names are rejected. A client that
    sent a tampered price simply hits the structured AMOUNT_MISMATCH and
    retries with the server total.
    """
    detail = (listing.record or {}).get('detail') or {}

    catalogue = {}
    for addon in detail.get('addons') or []:
        name = str(addon.get('name') or '').strip().lower()
        if name:
            catalogue[name] = _to_int(addon.get('price') or 0, 'addon price')
    packages = {}
    for package in detail.get('packages') or []:
        label = str(package.get('label') or package.get('name') or '').strip().lower()
        if label:
            packages[label] = _to_int(package.get('price') or 0, 'package price')
    extra_raw = str(detail.get('extraPersonPrice') or '').strip()
    extra_price = _to_int(extra_raw, 'extraPersonPrice') if extra_raw else None
    max_raw = str(detail.get('maxExtraPersons') or '').strip()
    max_extra = _to_int(max_raw, 'maxExtraPersons') if max_raw else 0

    addon_total = 0
    cleaned = []
    for line in requested_addons or []:
        name = str(line.get('name') or '').strip()
        if not name:
            raise SlotError('Each add-on needs a name.')
        qty = _to_int(line.get('qty') or 1, 'addon qty')
        if qty < 1:
            raise SlotError('addon qty must be at least 1.')

        key = name.lower()
        package_key = None
        for prefix in ('package — ', 'package - '):
            if key.startswith(prefix):
                package_key = key[len(prefix):].strip()
                break

        if key in catalogue:
            price = catalogue[key]
        elif package_key is not None and package_key in packages:
            price = packages[package_key]
        elif key in packages:
            price = packages[key]
        elif 'extra person' in key:
            if extra_price is None:
                raise SlotError('This venue does not charge for extra persons.')
            if max_extra and qty > max_extra:
                raise SlotError(f'Maximum {max_extra} extra persons allowed.')
            price = extra_price
        else:
            raise SlotError(f'Unknown add-on: "{name}".')

        addon_total += price * qty
        cleaned.append({'name': name, 'qty': qty, 'price': price})
    return addon_total, cleaned


def _parse_iso_date(text):
    try:
        return datetime.date.fromisoformat(str(text or '').strip())
    except ValueError:
        return None  # unparseable/empty -> open-ended


def _apply_platform_offer(base, offer_request):
    """
    A PLATFORM promo (source: "platform") validates against the ACTIVE admin
    banners instead of the venue's offers: matching code, today inside the
    from–to window, minAmount/maxDiscount guardrails. The vendor is made whole
    at payout time — the platform funds these discounts.
    """
    code = str(offer_request.get('code') or '').strip().upper()
    if not code:
        raise SlotError('This promo code is not valid.')

    try:
        from adminpanel.models import Settings
        banners = Settings.load().banners or []
    except Exception:
        banners = []

    today = today_ist()
    matched = None
    for banner in banners:
        if not isinstance(banner, dict):
            continue
        if str(banner.get('code') or '').strip().upper() != code:
            continue
        start = _parse_iso_date(banner.get('from'))
        end = _parse_iso_date(banner.get('to'))
        if start is not None and start > today:
            continue
        if end is not None and end < today:
            continue
        matched = banner
        break
    if matched is None:
        raise SlotError('This promo code is not valid or has expired.')

    min_amount = _to_int(matched.get('minAmount') or 0, 'minAmount')
    if base < min_amount:
        raise SlotError(f'This promo needs a minimum spend of ₹{min_amount}.')

    offer_type = str(matched.get('type') or '').strip().lower()
    value = _to_int(matched.get('value') or 0, 'promo value')
    if offer_type == 'percent':
        discount = round(base * value / 100)
        cap = _to_int(matched.get('maxDiscount') or 0, 'maxDiscount')
        if cap > 0:
            discount = min(discount, cap)
    else:  # flat
        discount = min(value, base)
    discount = min(discount, base)
    if discount <= 0:
        raise SlotError('This promo is not applicable to this booking.')

    applied = {
        'code': code,
        'title': matched.get('title') or '',
        'type': offer_type,
        'value': value,
        'source': 'platform',   # payout logic pays the vendor as if undiscounted
    }
    return discount, applied


def _apply_offer(listing, base, offer_request):
    """
    Validate the requested coupon and return (discount, applied_offer) —
    discount in ₹, applied_offer a {code,title,type,value,source} dict (or
    None when no offer was sent). source "platform" -> validated against the
    active admin banners; anything else -> the venue's stored `detail.offers`.

    `base` = slots + add-ons (NEVER the fee). Raises SlotError (-> 400) when the
    offer is unknown / expired / below its minimum spend.
    """
    if not offer_request or not isinstance(offer_request, dict):
        return 0, None

    if str(offer_request.get('source') or '').strip().lower() == 'platform':
        return _apply_platform_offer(base, offer_request)

    code = str(offer_request.get('code') or '').strip().upper()
    catalog = (listing.record.get('detail') or {}).get('offers') or []
    matched = next(
        (o for o in catalog if str(o.get('code') or '').strip().upper() == code),
        None,
    )
    if matched is None:
        if code == '':
            return 0, None  # no auto-apply ("") offer for this venue -> no discount
        raise SlotError('This offer is not valid for this venue.')

    expiry = str(matched.get('expiry') or '').strip()
    if expiry:
        try:
            parsed_expiry = datetime.date.fromisoformat(expiry)
        except ValueError:
            parsed_expiry = None  # unparseable -> treat as no expiry
        # NB: raise OUTSIDE the try — SlotError subclasses ValueError and would
        # otherwise be swallowed by the except above.
        if parsed_expiry is not None and parsed_expiry < today_ist():
            raise SlotError('This offer has expired.')

    min_amount = _to_int(matched.get('minAmount') or 0, 'minAmount')
    if base < min_amount:
        raise SlotError(f'This offer needs a minimum spend of ₹{min_amount}.')

    offer_type = str(matched.get('type') or '').strip().lower()
    value = _to_int(matched.get('value') or 0, 'offer value')
    if offer_type == 'percent':
        discount = round(base * value / 100)  # banker's rounding, matches frontend
        cap = matched.get('maxDiscount')
        if str(cap or '').strip():
            discount = min(discount, _to_int(cap, 'maxDiscount'))
    else:  # flat
        discount = min(value, base)
    discount = min(discount, base)  # never exceed the discountable base
    if discount <= 0:
        # A matched offer must actually discount something — a zero-value
        # "applied offer" on the receipt would be misleading.
        raise SlotError('This offer is not applicable to this booking.')

    applied = {
        'code': matched.get('code') or '',
        'title': matched.get('title') or '',
        'type': offer_type,
        'value': value,
        'source': 'venue',   # vendor-funded — payout unchanged
    }
    return discount, applied


def compute_amount(listing, intervals, requested_addons, rate=None,
                   offer_request=None, weekend=False):
    """
    Server-side total:
        base     = round(rate x minutes / 60) + sum(addon.price x qty)
        discount = coupon applied to `base` (slots + add-ons only)
        amount   = max(0, base - discount) + fee   (the ₹ fee is NOT discounted)

    `rate` is the per-unit rate when given, else the listing price. Returns
    (amount, cleaned_addons, applied_offer, discount, fee) — `fee` is what was
    actually charged, stored on the booking for payout math.
    """
    if rate is None:
        rate = base_rate(listing, weekend)
    slot_base = round(rate * total_minutes(intervals) / 60)
    addon_total, cleaned = _addon_total(listing, requested_addons)
    base = slot_base + addon_total

    discount, applied_offer = _apply_offer(listing, base, offer_request)
    fee = _booking_fee()
    amount = max(0, base - discount) + fee
    return amount, cleaned, applied_offer, discount, fee


def _slot_start(text):
    try:
        return parse_slots([text])[0][0]
    except SlotError:
        return 0


def _rates_for(listing, date):
    """The effective rates for this DATE — what a booking will actually be
    charged, so the client never has to guess weekday vs weekend."""
    weekend = is_weekend(date)
    detail = listing.record.get('detail') or {}
    unit_rates = []
    for entry in detail.get('sports') or []:
        name = str(entry.get('name') or '')
        for unit in range(1, int(entry.get('units') or 1) + 1):
            unit_rates.append({
                'sport': name, 'unit': unit,
                'rate': _unit_rate(listing, name, unit, weekend),
            })
    if not unit_rates:
        for unit in range(1, len(detail.get('unitPrices') or []) + 1):
            unit_rates.append({
                'sport': None, 'unit': unit,
                'rate': _unit_rate(listing, '', unit, weekend),
            })
    return {
        'rate': base_rate(listing, weekend),
        'isWeekend': weekend,
        'unitRates': unit_rates,
    }


def _availability(listing, date):
    """
    Availability payload. Each listing is ONE bookable resource in the
    frontend's model (pitches/screens are separate sibling listings), so the
    flat `booked` array lists the ranges of EVERY booking on this listing+date
    — the same rows the booking-create overlap check reads. The sport/unit
    fields bookings carry are display metadata and must never hide a range.
    `bookedUnits` groups the same ranges per (sport, unit) for detail views.
    """
    booked = set()
    per_unit = defaultdict(list)         # (sport, unit) -> [slot strings]

    for booking in _active_bookings(listing, date):
        booked.update(booking.slots)
        if booking.unit is not None:
            per_unit[(booking.sport or '', booking.unit)].extend(booking.slots)

    booked_units = [
        {'sport': sport or None, 'unit': unit, 'ranges': sorted(ranges, key=_slot_start)}
        for (sport, unit), ranges in sorted(per_unit.items(), key=lambda kv: (kv[0][0], kv[0][1]))
    ]

    payload = {
        'date': date.isoformat(),
        'booked': sorted(booked, key=_slot_start),
        'bookedUnits': booked_units,
    }
    payload.update(_rates_for(listing, date))   # rate / isWeekend / unitRates
    return payload


class AvailabilityView(APIView):
    """GET /api/venues/<id>/availability?date=YYYY-MM-DD (public).

    NOT cached on purpose — a stale "free" slot would be a lie."""

    permission_classes = [AllowAny]

    def get(self, request, listing_id):
        listing = Listing.objects.filter(
            pk=listing_id, status=Listing.Status.LIVE
        ).first()
        if listing is None:
            return _message('Venue not found.', status.HTTP_404_NOT_FOUND)

        try:
            date = parse_date(request.query_params.get('date'))
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)

        if date < today_ist():
            return _message('date cannot be in the past.', status.HTTP_400_BAD_REQUEST)

        return Response(_availability(listing, date))


def validate_booking_request(body):
    """
    Full server-side validation + pricing for a customer booking request.
    Shared by direct booking creation AND Razorpay order creation so the two
    can never disagree. Returns (data_dict, None) on success or
    (None, error_response) on failure.
    """
    # --- Resolve the venue -------------------------------------
    listing = None
    venue_id = body.get('venueId') or body.get('listingId')
    if venue_id:
        listing = Listing.objects.filter(
            pk=str(venue_id), status=Listing.Status.LIVE
        ).first()
    elif body.get('venueName'):
        matches = list(Listing.objects.filter(
            name=str(body['venueName']), status=Listing.Status.LIVE
        )[:2])
        listing = matches[0] if len(matches) == 1 else None
    if listing is None:
        return None, _message('Venue not found.', status.HTTP_404_NOT_FOUND)

    # Vendor-paused venue: visible, but not taking customer bookings.
    if _vendor_paused(listing):
        return None, _message(
            'This venue is temporarily not accepting bookings',
            status.HTTP_409_CONFLICT,
        )

    # --- Validate date, slots, unit & amount -------------------
    try:
        date = parse_date(body.get('date'))
        intervals = parse_slots(body.get('slots'))
        sport, unit, unit_label = _parse_unit(body)
        # Rate depends on the DATE BEING BOOKED: weekend prices apply on
        # Sat/Sun (IST), falling back to the weekday rate when unset.
        weekend = is_weekend(date)
        rate = _unit_rate(listing, sport, unit, weekend)  # None -> base rate
        amount, addons, applied_offer, discount, fee = compute_amount(
            listing, intervals, body.get('addons'), rate=rate,
            offer_request=body.get('offer'), weekend=weekend,
        )
        client_amount = _to_int(body.get('amount'), 'amount')
        per_slot = _to_int(
            body.get('perSlot') or listing.record.get('price') or 0, 'perSlot'
        )
    except SlotError as error:
        return None, _message(str(error), status.HTTP_400_BAD_REQUEST)

    today = today_ist()
    if date < today:
        return None, _message('Cannot book a past date.', status.HTTP_400_BAD_REQUEST)
    if date == today:
        first_start = min(start for start, _ in intervals)
        if first_start <= now_minutes_ist():
            return None, _message(
                'That time has already passed today.', status.HTTP_400_BAD_REQUEST
            )

    # Payment method — stored and echoed EXACTLY as received ('online'
    # only when the client sends none).
    method = str(body.get('method') or Booking.Method.ONLINE)
    if method not in Booking.CUSTOMER_METHODS:
        return None, _message(
            'method must be one of: online, upi, card, netbanking, venue.',
            status.HTTP_400_BAD_REQUEST,
        )

    # Cross-check the client's INFORMATIONAL discountAmount (money math
    # always uses our recomputed value).
    client_discount = body.get('discountAmount')
    if client_discount not in (None, ''):
        try:
            client_discount = int(str(client_discount))
        except (TypeError, ValueError):
            client_discount = None
        if client_discount is not None and abs(client_discount - discount) > 1:
            return None, Response(
                {
                    'message': 'Offer amount mismatch — please retry the booking',
                    'code': 'OFFER_MISMATCH',
                    'discountAmount': discount,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    # SECURITY: the client's amount is only ACCEPTED, never trusted.
    if client_amount != amount:
        return None, _amount_mismatch(amount)

    return {
        'listing': listing, 'date': date, 'intervals': intervals,
        'sport': sport, 'unit': unit, 'unit_label': unit_label,
        'addons': addons, 'applied_offer': applied_offer,
        'discount': discount, 'fee': fee, 'amount': amount,
        'per_slot': per_slot, 'method': method,
    }, None


def build_booking_fields(user, body, data):
    """The Booking.objects.create kwargs shared by all creation paths."""
    listing = data['listing']
    return dict(
        listing=listing,
        user=user,
        venue_name=listing.record.get('name') or listing.name,
        category=listing.category,
        location=str(listing.record.get('location') or ''),
        image=str(listing.record.get('image') or ''),
        customer_name=str(body.get('customer') or user.name),
        phone=user.phone,
        sport=data['sport'],
        unit=data['unit'],
        unit_label=data['unit_label'],
        date=data['date'],
        slots=[str(slot) for slot in body['slots']],
        per_slot=data['per_slot'],
        addons=data['addons'],
        offer=data['applied_offer'],
        discount_amount=data['discount'],
        fee=data['fee'],
        amount=data['amount'],
        method=data['method'],
    )


class MyBookingsView(APIView):
    """
    GET  /api/users/me/bookings — my bookings (?status=upcoming|past)
    POST /api/users/me/bookings — confirm a booking (blocks the slots)
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Unpaid Razorpay holds are checkout plumbing, not bookings.
        # select_related('rating') so userRating costs no extra query per row.
        queryset = Booking.objects.filter(user=request.user).select_related(
            'rating'
        ).exclude(status='payment_pending')

        wanted = request.query_params.get('status')
        if wanted == 'upcoming':
            queryset = queryset.filter(date__gte=today_ist())
        elif wanted == 'past':
            queryset = queryset.filter(date__lt=today_ist())
        elif wanted:
            return _message('status must be "upcoming" or "past".', status.HTTP_400_BAD_REQUEST)

        try:
            limit = int(request.query_params.get('limit', DEFAULT_LIMIT))
            page = int(request.query_params.get('page', 1))
        except ValueError:
            return _message('page and limit must be numbers.', status.HTTP_400_BAD_REQUEST)
        if not (1 <= limit <= MAX_LIMIT) or page < 1:
            return _message('Invalid page or limit.', status.HTTP_400_BAD_REQUEST)

        total = queryset.count()
        rows = queryset[(page - 1) * limit:page * limit]
        return Response({
            'bookings': [row.as_record() for row in rows],
            'total': total,
        })

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}

        data, error = validate_booking_request(body)
        if error is not None:
            return error
        listing, date = data['listing'], data['date']
        intervals, sport, unit = data['intervals'], data['sport'], data['unit']

        # --- The race-safe part ------------------------------------
        with transaction.atomic():
            # Lock this venue's row: concurrent bookings for the same
            # venue now wait here and run strictly one at a time.
            Listing.objects.select_for_update().get(pk=listing.pk)

            if overlaps(intervals, _booked_intervals(listing, date, sport, unit)):
                return _message(
                    'One or more selected time slots were just booked. '
                    'Please pick different slots.',
                    status.HTTP_409_CONFLICT,
                )

            booking = Booking.objects.create(
                **build_booking_fields(request.user, body, data)
            )

        # Booking makes you a customer, whatever your role (admin Users list).
        if not request.user.is_customer:
            request.user.is_customer = True
            request.user.save(update_fields=['is_customer'])

        return Response({'booking': booking.as_record()}, status=status.HTTP_201_CREATED)


class CancelBookingView(APIView):
    """DELETE /api/users/me/bookings/<id> — cancel an upcoming booking."""

    permission_classes = [IsAuthenticated]

    def delete(self, request, booking_id):
        # Owner-only: someone else's booking id looks like it doesn't exist.
        booking = Booking.objects.filter(pk=booking_id, user=request.user).first()
        if booking is None:
            return _message('Booking not found.', status.HTTP_404_NOT_FOUND)

        # Time-based, not date-based: a same-day booking whose last slot has
        # ENDED can no longer be cancelled.
        today = today_ist()
        ended_today = (
            booking.date == today
            and (end := slots_end_minute(booking.slots))
            and end <= now_minutes_ist()
        )
        if booking.date < today or ended_today:
            return _message('This booking is already completed.', status.HTTP_400_BAD_REQUEST)

        if booking.razorpay_order_id:
            # Gateway-linked (paid OR unpaid soft-hold): keep the row so a late
            # payment.captured webhook can still find it and auto-refund —
            # deleting it would strand the customer's money. status 'cancelled'
            # frees the slots instantly (excluded from _active_bookings).
            booking.status = 'cancelled'
            booking.save(update_fields=['status'])
        else:
            booking.delete()  # legacy/no-gateway — freed slots reappear instantly
        return Response({'cancelled': True, 'id': booking_id})
