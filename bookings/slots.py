"""
ONE place that understands time slots.

Slot format (frontend contract): "HH:MM – HH:MM" (24h, en dash),
end "00:00" means midnight. Business hours 06:00-24:00, 30-minute
granularity, minimum 30 minutes.

Used by booking creation, availability, and walk-ins — so the rules
can never disagree between endpoints.
"""
import datetime
import re
from zoneinfo import ZoneInfo

# The product is India-only; "venue local time" = IST.
IST = ZoneInfo('Asia/Kolkata')

OPEN_MINUTE = 6 * 60      # 06:00
CLOSE_MINUTE = 24 * 60    # midnight
STEP_MINUTES = 30

# Accepts an en dash (what the frontend sends) or a plain hyphen.
_SLOT_RE = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})\s*$')


class SlotError(ValueError):
    """Raised with a human-readable message for any invalid slot."""


def _to_minutes(hours, minutes, *, is_end):
    if minutes >= 60 or hours > 24:
        raise SlotError('Invalid time in slot.')
    total = hours * 60 + minutes
    if is_end and total == 0:
        total = CLOSE_MINUTE  # "00:00" as an END means midnight
    return total


def parse_slot(text):
    """'19:30 – 21:00' -> (1170, 1260). Raises SlotError when invalid."""
    match = _SLOT_RE.match(str(text))
    if not match:
        raise SlotError(f'Invalid slot format: "{text}". Use "HH:MM – HH:MM".')

    start = _to_minutes(int(match[1]), int(match[2]), is_end=False)
    end = _to_minutes(int(match[3]), int(match[4]), is_end=True)

    if start % STEP_MINUTES or end % STEP_MINUTES:
        raise SlotError('Times must be on 30-minute boundaries.')
    if end <= start:
        raise SlotError('Slot end must be after its start.')
    if end - start < STEP_MINUTES:
        raise SlotError('Minimum booking is 30 minutes.')
    if start < OPEN_MINUTE or end > CLOSE_MINUTE:
        raise SlotError('Slots must be within business hours (06:00-24:00).')

    return start, end


def parse_slots(texts):
    """Parse a list of slot strings; also rejects slots that overlap
    EACH OTHER within the same request."""
    if not isinstance(texts, list) or not texts:
        raise SlotError('At least one time slot is required.')

    intervals = [parse_slot(text) for text in texts]
    ordered = sorted(intervals)
    for previous, current in zip(ordered, ordered[1:]):
        if current[0] < previous[1]:
            raise SlotError('Selected slots overlap each other.')
    return intervals


def overlaps(intervals_a, intervals_b):
    """True if ANY interval in a touches ANY interval in b."""
    return any(
        a_start < b_end and b_start < a_end
        for a_start, a_end in intervals_a
        for b_start, b_end in intervals_b
    )


def total_minutes(intervals):
    return sum(end - start for start, end in intervals)


def parse_date(text):
    """'2026-07-21' -> date. Raises SlotError when invalid."""
    try:
        return datetime.date.fromisoformat(str(text))
    except ValueError:
        raise SlotError('date must look like YYYY-MM-DD.')


def today_ist():
    return datetime.datetime.now(IST).date()


def now_minutes_ist():
    now = datetime.datetime.now(IST)
    return now.hour * 60 + now.minute
