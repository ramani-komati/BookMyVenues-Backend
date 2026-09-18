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
