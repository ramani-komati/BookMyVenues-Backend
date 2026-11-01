"""
The coarse demand band shown on date strips and slot buttons:

    available      half or more of the places are still free
    filling_fast   under half free
    almost_full    a fifth or less free
    sold_out       nothing free

ONE rule for venue days (bookings/day_summary.py) and event nights
(events/public_views.date_rows), so the same words always mean the same
thing. Only the band leaves the server for events — never a seat count
(see events/public_views.date_rows).
"""

FILLING_FAST_BELOW = 0.5     # free share under this -> filling fast
ALMOST_FULL_AT = 0.2         # free share at or under this -> almost full

BANDS = ('available', 'filling_fast', 'almost_full', 'sold_out')


def demand_band(free, total):
    """The band for `free` of `total` places, or None when there is no
    finite capacity to measure against (an unlimited event night)."""
    if total is None or total <= 0:
        return None
    free = max(0, min(free, total))
    if free == 0:
        return 'sold_out'
    share = free / total
    if share <= ALMOST_FULL_AT:
        return 'almost_full'
    if share < FILLING_FAST_BELOW:
        return 'filling_fast'
    return 'available'
