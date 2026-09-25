"""
Per-venue part payment: the customer pays a slice online now and the rest in
cash at the venue.

The split is duplicated in the customer app, so the two MUST agree exactly or
every part-paid booking dies on the amount check. This module is the server's
single copy of that formula — nothing else should compute a split.

    raw     = fixed ? min(value, total) : round(total * value / 100)
    payNow  = min(total, max(raw, min(fee, total)))
    atVenue = total - payNow

The floor is the point: payNow is never less than the platform fee, so our
fee is always covered by the online slice and a payout is never negative.
Every figure is whole rupees.
"""
PERCENT = 'percent'
FIXED = 'fixed'

MIN_PERCENT = 1
MAX_PERCENT = 99


def read_config(record):
    """The venue's partPayment settings, or None when it is off.

    Returns None for anything malformed rather than guessing — a venue with a
    broken config charges the full amount online, which is the safe direction
    to fail in.
    """
    config = (record or {}).get('partPayment')
    if not isinstance(config, dict):
        return None
    if not config.get('enabled'):
        return None

    mode = str(config.get('mode') or '').strip().lower()
    if mode not in (PERCENT, FIXED):
        return None
    try:
        value = int(float(str(config.get('value'))))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if mode == PERCENT and not (MIN_PERCENT <= value <= MAX_PERCENT):
        return None

    return {'enabled': True, 'mode': mode, 'value': value}


def config_for_listing(listing):
    """The split that applies when booking THIS listing.

    A theatre screen or a turf court is a unit sibling: its own listing row
    carries `detail.unitOf` pointing at the venue it belongs to. Part payment
    is configured once, on that parent venue — so a sibling with no config of
    its own inherits the parent's, otherwise booking screen 2 of a part-pay
    venue would quietly charge the full amount.

    An explicit config on the sibling still wins, so a single screen can be
    given its own terms later without the parent overriding it.

    The parent is safe to trust: `unitOf` is validated at publish time to
    point at one of the same vendor's venues, so this cannot inherit a split
    from a stranger's listing.
    """
    own = read_config(getattr(listing, 'record', None))
    if own is not None:
        return own

    detail = (getattr(listing, 'record', None) or {}).get('detail') or {}
    unit_of = str(detail.get('unitOf') or '').strip()
    if not unit_of:
        return None

    from django.core.exceptions import ValidationError
    from venues.models import Listing
    try:
        base = Listing.objects.filter(pk=unit_of).only('record').first()
    except (ValidationError, ValueError, TypeError):
        # Malformed unitOf: no inheritance. Failing to FULL payment is the
        # safe direction — never charge less because a field was garbled.
        return None
    return read_config(base.record) if base is not None else None


def split(total, config, fee):
    """(pay_now, at_venue) for a booking total. No config -> pay it all now."""
    if not config or total <= 0:
        return total, 0

    if config['mode'] == FIXED:
        raw = min(config['value'], total)
    else:
        raw = round(total * config['value'] / 100)

    pay_now = min(total, max(raw, min(fee, total)))
    return pay_now, total - pay_now
