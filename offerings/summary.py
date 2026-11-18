"""
How a membership / package / surprise booking describes itself on every
booking record (Booking.as_record → `offering`), and a membership's state.
"""
from bookings.slots import today_ist

from .availability import clock


def membership_state(membership, booking_status, today=None):
    """'pending' | 'upcoming' | 'active' | 'used_up' | 'expired' — or the
    booking's own status when the money side ended it (cancelled, refunded…).
    Only a CONFIRMED booking is ever a valid membership."""
    if booking_status != 'confirmed':
        return 'pending' if booking_status in ('payment_pending', 'failed') else booking_status
    today = today or today_ist()
    if membership.ends_on < today:
        return 'expired'
    if (membership.visits_allowed is not None
            and membership.visits_used >= membership.visits_allowed
            and membership.plan_snapshot.get('expiryRule') == 'visits_or_duration'):
        return 'used_up'
    if membership.starts_on > today:
        return 'upcoming'
    return 'active'


def membership_payload(membership, booking):
    snapshot = membership.plan_snapshot or {}
    remaining = None
    if membership.visits_allowed is not None:
        remaining = max(0, membership.visits_allowed - membership.visits_used)
    return {
        'type': 'membership',
        'title': snapshot.get('name') or 'Membership',
        'plan': snapshot,
        'startsOn': membership.starts_on.isoformat(),
        'endsOn': membership.ends_on.isoformat(),
        'visitsAllowed': membership.visits_allowed,
        'visitsUsed': membership.visits_used,
        'visitsRemaining': remaining,
        'joiningFee': membership.joining_fee,
        'facilities': snapshot.get('facilities') or [],
        'state': membership_state(membership, booking.status),
    }


def offering_summary(booking):
    """None for ordinary slot bookings (no query is made for them)."""
    kind = getattr(booking, 'kind', '')
    if not kind:
        return None
    if kind == 'membership':
        membership = getattr(booking, 'membership', None)
        return membership_payload(membership, booking) if membership else {'type': kind}
    if kind == 'package':
        detail = getattr(booking, 'package_booking', None)
        if detail is None:
            return {'type': kind}
        snapshot = detail.package_snapshot or {}
        return {
            'type': 'package',
            'title': snapshot.get('name') or 'Resort package',
            'package': snapshot,
            'adults': detail.adults,
            'children': detail.children,
            'persons': detail.persons,
            'slot': f'{clock(detail.start_minute)} – {clock(detail.end_minute)}',
            'inclusions': snapshot.get('inclusions') or [],
        }
    if kind == 'surprise':
        detail = getattr(booking, 'surprise_booking', None)
        if detail is None:
            return {'type': kind}
        return {
            'type': 'surprise',
            'title': f'Big screen surprise — {detail.occasion}',
            'movie': detail.movie,
            'screen': detail.screen,
            'showTime': clock(detail.show_minute),
            'occasion': detail.occasion,
            'recipientName': detail.recipient_name,
            'message': detail.message,
            'senderName': detail.sender_name,
            'photoUrl': detail.photo_url or None,
            'videoUrl': detail.video_url or None,
        }
    return {'type': kind}
