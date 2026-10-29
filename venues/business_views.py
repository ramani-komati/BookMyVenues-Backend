"""
Vendor business controls API (rules and enforcement: venues/business.py).

    GET    /api/vendors/me/venues/<id>/business            settings + resources
    PUT    /api/vendors/me/venues/<id>/business/schedule    hours, special dates,
                                                            closures, instructions
    POST   /api/vendors/me/venues/<id>/business/schedule/preview
                                                            bookings a schedule
                                                            change would affect
    PUT    /api/vendors/me/venues/<id>/business/pricing     price rules
    POST   /api/vendors/me/venues/<id>/business/pricing/preview
                                                            price bands for a date
                                                            (saved or unsaved rules)
    GET    /api/vendors/me/venues/<id>/calendar?date=&days= bookings, blocks,
                                                            closed times, prices
    POST   /api/vendors/me/venues/<id>/blocks               block a time range
    DELETE /api/vendors/me/venues/<id>/blocks/<blockId>     reopen it

<id> may be the venue or any of its pitch / screen listings — all resolve to
the base venue. SECURITY: every route is scoped to the token's own listings,
so another vendor's venue is a 404, never a 403 that confirms it exists.
Writes carry the `revision` they were based on; a stale one is a 409 with the
current settings, so two open tabs can't overwrite each other.
"""
import datetime

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from bookings.slots import SlotError, today_ist

from .business import (
    MAX_CALENDAR_DAYS,
    BusinessError,
    Conflicts,
    StaleRevision,
    base_listing,
    block_record,
    calendar,
    clock,
    create_block,
    delete_block,
    labels_for,
    open_periods,
    parse_day,
    price_bands,
    save_price_rules,
    save_schedule,
    settings_payload,
    validate_price_rules,
    venue_set,
)
from .models import Listing, VenueBusinessSettings
from .views import IsVendor


def _message(text, http_status, **extra):
    return Response({'message': text, **extra}, status=http_status)


def _body(request):
    return request.data if isinstance(request.data, dict) else {}


def _own_base(request, listing_id):
    """The caller's venue (resolved to its base), or None."""
    listing = (
        request.user.listings.exclude(status=Listing.Status.DELETED)
        .filter(pk=listing_id).first()
    )
    if listing is None:
        return None
    base = base_listing(listing)
    return base if base.vendor_id == request.user.id else None


def _not_found():
    return _message('Venue not found.', status.HTTP_404_NOT_FOUND)


def _stale(base):
    return _message(
        'These settings were changed in another window. The latest version '
        'has been loaded — review it and save again.',
        status.HTTP_409_CONFLICT, code='STALE', settings=settings_payload(base),
    )


def _invalid(error):
    return _message(str(error), status.HTTP_400_BAD_REQUEST, code=error.code)


class VendorBusinessView(APIView):
    permission_classes = [IsVendor]

    def get(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        return Response(settings_payload(base))


class VendorScheduleView(APIView):
    permission_classes = [IsVendor]

    def put(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        try:
            result = save_schedule(base, request.user, _body(request))
        except BusinessError as error:
            return _invalid(error)
        except StaleRevision:
            return _stale(base)
        except Conflicts as conflict:
            return _message(
                conflict.message, status.HTTP_409_CONFLICT,
                code='BOOKING_CONFLICTS', conflicts=conflict.bookings,
            )
        return Response({**result['settings'], 'keptBookings': result['conflicts']})


class VendorSchedulePreviewView(APIView):
    """Dry run: which existing bookings would this schedule leave outside
    the hours? Nothing is saved."""

    permission_classes = [IsVendor]

    def post(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        try:
            result = save_schedule(base, request.user, _body(request), dry_run=True)
        except BusinessError as error:
            return _invalid(error)
        except StaleRevision:
            return _stale(base)
        return Response({'conflicts': result['conflicts']})


class VendorPricingView(APIView):
    permission_classes = [IsVendor]

    def put(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        try:
            return Response(save_price_rules(base, request.user, _body(request)))
        except BusinessError as error:
            return _invalid(error)
        except StaleRevision:
            return _stale(base)


class VendorPricingPreviewView(APIView):
    """What a customer would pay on `date` for one pitch/screen, under the
    saved rules or the unsaved `priceRules` in the body. Nothing is saved."""

    permission_classes = [IsVendor]

    def post(self, request, listing_id):
        from bookings.views import _rates_for

        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        body = _body(request)
        listings = venue_set(base)
        by_id = {str(l.pk): l for l in listings}
        target = by_id.get(str(body.get('resourceId') or base.pk))
        if target is None:
            return _message('Pick a pitch/screen of this venue.', status.HTTP_400_BAD_REQUEST)
        try:
            day = parse_day(body.get('date') or today_ist().isoformat(), label='Date')
            settings = VenueBusinessSettings.objects.filter(listing=base).first()
            if 'priceRules' in body:
                draft = settings or VenueBusinessSettings(listing=base)
                draft.price_rules = validate_price_rules(body.get('priceRules'), list(by_id))
                settings = draft
            default = _rates_for(target, day)['rate']
        except BusinessError as error:
            return _invalid(error)
        except SlotError as error:
            return _message(str(error), status.HTTP_400_BAD_REQUEST)
        return Response({
            'date': day.isoformat(),
            'resourceId': str(target.pk),
            'resourceLabel': labels_for(listings)[str(target.pk)],
            'defaultRate': default,
            'isWeekend': day.weekday() >= 5,
            'bands': price_bands(target, day, settings=settings, default_rate=default),
            # The day's opening periods ([] = closed), so the preview shows
            # only the hours customers can actually book.
            'open': [{'start': clock(s), 'end': clock(e)} for s, e in open_periods(settings, day)[0]],
        })


class VendorCalendarView(APIView):
    permission_classes = [IsVendor]

    def get(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        try:
            start = parse_day(
                request.query_params.get('date') or today_ist().isoformat(), label='Date'
            )
            days = int(request.query_params.get('days') or 1)
        except BusinessError as error:
            return _invalid(error)
        except ValueError:
            return _message('days must be a number.', status.HTTP_400_BAD_REQUEST)
        if not 1 <= days <= MAX_CALENDAR_DAYS:
            return _message(f'days must be 1 to {MAX_CALENDAR_DAYS}.', status.HTTP_400_BAD_REQUEST)
        if start < today_ist() - datetime.timedelta(days=90):
            return _message('The calendar goes back 90 days.', status.HTTP_400_BAD_REQUEST)
        return Response(calendar(base, start, days))


class VendorBlocksView(APIView):
    permission_classes = [IsVendor]

    def post(self, request, listing_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        if base.status != Listing.Status.LIVE:
            return _message(
                'Blocking times is available once the venue is live.',
                status.HTTP_400_BAD_REQUEST,
            )
        try:
            block = create_block(base, request.user, _body(request))
        except BusinessError as error:
            return _invalid(error)
        except Conflicts as conflict:
            return _message(
                conflict.message, status.HTTP_409_CONFLICT,
                code='BOOKING_CONFLICTS', conflicts=conflict.bookings,
            )
        return Response(
            {'block': block_record(block, labels_for(venue_set(base)))},
            status=status.HTTP_201_CREATED,
        )


class VendorBlockDetailView(APIView):
    permission_classes = [IsVendor]

    def delete(self, request, listing_id, block_id):
        base = _own_base(request, listing_id)
        if base is None:
            return _not_found()
        try:
            deleted = delete_block(base, block_id)
        except BusinessError as error:
            return _invalid(error)
        if deleted is None:
            return _message('Block not found.', status.HTTP_404_NOT_FOUND)
        return Response({'reopened': True, 'id': block_id})
