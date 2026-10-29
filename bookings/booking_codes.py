"""
ONE booking ID for every booking type: venue bookings of every category
(party halls, private theatres, play zones, resorts, PlayStation venues,
walk-ins …) and event ticket orders alike — 6 characters, e.g. "K7M2QX".

It is the format event tickets already used (events.models.make_order_id):
uppercase letters and digits minus the look-alikes 0/O and 1/I/L, and never
all-hex — so a new ID can never equal the "#3F8A92" / "BMV-3F8A92" short
reference that older venue bookings print on invoices.

UNIQUE ACROSS ALL BOOKING TYPES, enforced by the database: every new
6-character ID is also inserted into BookingCode (code = primary key) inside
the SAME savepoint as the booking / order row. Venue bookings and event orders
live in separate tables, but they can never be handed the same ID — not even
by two simultaneous requests.

Rows created before this keep the IDs they were created with ('bk_…' venue
bookings, 'eo_…' event orders). A primary key is referenced by ratings,
payment records and customers' history, so it is never rewritten; those older
IDs remain fully verifiable.
"""
import re
import secrets

from django.db import IntegrityError, transaction

ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
LENGTH = 6
_NON_HEX = frozenset(ALPHABET) - frozenset('0123456789ABCDEF')
_CODE = re.compile(r'[A-Z0-9]{6}')
ATTEMPTS = 10


def new_code():
    """A fresh 6-character booking ID from the OS's secure generator."""
    while True:
        code = ''.join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        if not _NON_HEX.isdisjoint(code):
            return code


def is_code(value):
    """True for a 6-character booking ID (the shared format)."""
    return bool(_CODE.fullmatch(str(value or '')))


def code_in_use(code):
    """Is `code` taken by ANY booking type (or reserved by an in-flight insert)?"""
    from events.models import EventOrder

    from .models import Booking, BookingCode

    return (
        BookingCode.objects.filter(pk=code).exists()
        or Booking.objects.filter(pk=code).exists()
        or EventOrder.objects.filter(pk=code).exists()
    )


def insert_with_code(instance, kind, insert):
    """Insert a NEW booking / order (`insert()` = the model's own save) under a
    platform-unique 6-character ID.

    A generated ID already in use anywhere is skipped up front; one claimed by
    a concurrent insert between that check and ours makes the BookingCode
    INSERT fail, the savepoint rolls back both rows, and we retry with a fresh
    code. An explicit ID (e.g. a legacy 'bk_…' in fixtures) and any other
    integrity error behave exactly as Django normally does.
    """
    from .models import BookingCode

    generated = getattr(instance, '_generated_id', False)
    for _ in range(ATTEMPTS):
        if generated and code_in_use(instance.pk):
            instance.pk = new_code()
            continue
        try:
            with transaction.atomic():
                if is_code(instance.pk):
                    BookingCode.objects.create(code=instance.pk, kind=kind)
                return insert()
        except IntegrityError:
            if not generated or not code_in_use(instance.pk):
                raise   # not an ID clash
            instance.pk = new_code()
    raise IntegrityError('Could not allocate a unique booking ID.')
