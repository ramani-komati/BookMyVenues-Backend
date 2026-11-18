"""
Invoice numbers — ONE per booking, issued by the backend exactly once.

    INV-2627-000123
        │    └─ serial: 1, 2, 3 … within the financial year, zero-padded to 6
        └────── the Indian financial year (1 April – 31 March, IST) the booking
                was CREATED in: 2026-27 → "2627"

One series for the whole platform: venue bookings (bookings.Booking) and
event ticket orders (events.EventOrder) draw from the same counter, so a
number is never issued twice across the two tables, and the series reads in
order of creation. Each table also has a UNIQUE constraint on the column as
the database-level backstop.

How a number is issued
  * Booking.save / EventOrder.save call insert_with_invoice() when a NEW row
    is inserted. The counter row (InvoiceSequence) is incremented with a
    single conditional UPDATE inside the SAME transaction as the booking
    insert. The UPDATE row-locks the counter until that transaction ends, so
    two simultaneous bookings are serialised: each gets the next number. If
    the booking insert fails, the increment rolls back with it — a failed
    insert never burns a number.
  * Nothing else assigns one. Reading, listing or opening a booking never
    does, and an existing number is never replaced.

What a number does NOT mean
  It records that the booking EXISTS, not that it was paid. Unpaid holds,
  failed attempts, cancelled and refunded bookings keep their number; the
  payment state is the booking's status and paid_at (bookings/payment_record).

Existing rows
  assign_missing_invoice_numbers() numbers every row that has none, oldest
  first, in the financial year of its own created_at. The bookings 0022 data
  migration runs it once; `python manage.py assign_invoice_numbers` re-runs it
  safely at any time (rows that already have a number are never touched).

This is a consistent, auditable numbering scheme. Whether it satisfies a
particular tax or accounting rule is for the business's accountant to
confirm — nothing here claims that it does.
"""
import re

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .slots import IST

PREFIX = 'INV'
SERIAL_DIGITS = 6
_NUMBER = re.compile(r'INV-(\d{4})-(\d+)')


def financial_year(moment):
    """'2627' for any moment from 1 Apr 2026 00:00 IST to 31 Mar 2027 23:59 IST."""
    local = timezone.localtime(moment, IST) if timezone.is_aware(moment) else moment
    start = local.year if local.month >= 4 else local.year - 1
    return f'{start % 100:02d}{(start + 1) % 100:02d}'


def format_invoice_number(year, serial):
    return f'{PREFIX}-{year}-{serial:0{SERIAL_DIGITS}d}'


def parse_invoice_number(value):
    """(financial_year, serial) for a well-formed number, else None."""
    match = _NUMBER.fullmatch(str(value or ''))
    return (match.group(1), int(match.group(2))) if match else None


def _sequence_model():
    from .models import InvoiceSequence
    return InvoiceSequence


def _claim_serial(sequence, year):
    """The next serial in `year`'s series. Must run inside a transaction: the
    UPDATE's row lock is what serialises concurrent callers, and it is held
    until the caller's transaction commits or rolls back."""
    with transaction.atomic():
        if sequence.objects.filter(pk=year).update(last_number=F('last_number') + 1):
            return sequence.objects.filter(pk=year).values_list('last_number', flat=True).get()
        # First number of a new financial year. Two requests can both get
        # here; the primary key lets exactly one create the row, and the
        # other falls back to incrementing it.
        try:
            with transaction.atomic():
                sequence.objects.create(financial_year=year, last_number=1)
            return 1
        except IntegrityError:
            sequence.objects.filter(pk=year).update(last_number=F('last_number') + 1)
            return sequence.objects.filter(pk=year).values_list('last_number', flat=True).get()


def next_invoice_number(moment=None, sequence=None):
    """Claim the next invoice number for a booking created at `moment` (now)."""
    sequence = sequence or _sequence_model()
    year = financial_year(moment or timezone.now())
    return format_invoice_number(year, _claim_serial(sequence, year))


def insert_with_invoice(instance, insert):
    """Insert a NEW booking / order (`insert()` = its normal save) with an
    invoice number claimed in the same transaction.

    A row given an explicit number (fixtures) is inserted as-is. If the insert
    fails, the claim rolls back with it and the instance forgets the number,
    so a retried save() claims a fresh one rather than reusing a number the
    counter has already handed back.
    """
    if instance.invoice_number:
        return insert()
    try:
        with transaction.atomic():
            instance.invoice_number = next_invoice_number()
            return insert()
    except BaseException:
        instance.invoice_number = None
        raise


# --- Existing rows ---------------------------------------------------------

def _missing(model):
    return model.objects.filter(Q(invoice_number__isnull=True) | Q(invoice_number=''))


def sync_sequences(models, sequence=None):
    """Raise each year's counter to at least the highest serial already used
    in `models`, so the next claim can never collide with an existing number
    (e.g. after rows were restored from a backup). Never lowers a counter."""
    sequence = sequence or _sequence_model()
    highest = {}
    for model in models:
        numbers = model.objects.exclude(
            Q(invoice_number__isnull=True) | Q(invoice_number='')
        ).values_list('invoice_number', flat=True)
        for number in numbers.iterator():
            parsed = parse_invoice_number(number)
            if parsed and parsed[1] > highest.get(parsed[0], 0):
                highest[parsed[0]] = parsed[1]
    for year, serial in highest.items():
        with transaction.atomic():
            sequence.objects.get_or_create(financial_year=year, defaults={'last_number': 0})
            sequence.objects.filter(pk=year, last_number__lt=serial).update(last_number=serial)
    return highest


class _AlreadyNumbered(Exception):
    """A row gained a number between listing and updating it (rollback the claim)."""


def assign_missing_invoice_numbers(booking_model=None, order_model=None, sequence=None):
    """Give every booking and event order WITHOUT an invoice number its own,
    oldest first, in the financial year of its created_at.

    Safe to repeat: rows that already have a number are never touched, ids
    and every other column are left exactly as they are, and nothing is
    created or deleted. Each row is numbered in its own small transaction
    (claim + conditional UPDATE), so an interrupted run loses nothing and a
    row numbered concurrently by someone else keeps that number — its claim
    is rolled back, leaving no gap. Accepts historical models so the data
    migration can use it. Returns how many rows were numbered.
    """
    if booking_model is None or order_model is None:
        from events.models import EventOrder

        from .models import Booking
        booking_model = booking_model or Booking
        order_model = order_model or EventOrder
    sequence = sequence or _sequence_model()

    sync_sequences((booking_model, order_model), sequence)

    pending = []
    for kind, model in (('venue', booking_model), ('event', order_model)):
        for pk, created in _missing(model).values_list('pk', 'created_at').iterator():
            pending.append((created or timezone.now(), kind, str(pk), model))
    pending.sort(key=lambda row: (row[0], row[1], row[2]))

    numbered = 0
    for created, _kind, pk, model in pending:
        try:
            with transaction.atomic():
                number = next_invoice_number(created, sequence)
                updated = _missing(model).filter(pk=pk).update(invoice_number=number)
                if not updated:
                    raise _AlreadyNumbered
            numbered += 1
        except _AlreadyNumbered:
            continue
    return numbered
