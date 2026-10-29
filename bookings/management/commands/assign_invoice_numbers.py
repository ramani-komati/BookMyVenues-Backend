"""
Give every venue booking and event ticket order that has no invoice number
its own — the same backfill the bookings 0022 migration runs once.

    python manage.py assign_invoice_numbers --dry-run
        Counts the rows without a number. Writes nothing.

    python manage.py assign_invoice_numbers
        Numbers them, oldest first, in the financial year of each row's own
        created_at (bookings/invoices.py).

Safe to run any number of times: a row that already has a number is never
touched, no booking is created or deleted, and ids, payment references and
every other column are left exactly as they were. Run it once after a deploy
if the old server took bookings while the migration was being applied.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from bookings.invoices import assign_missing_invoice_numbers
from bookings.models import Booking
from events.models import EventOrder


def _missing_count(model):
    return model.objects.filter(Q(invoice_number__isnull=True) | Q(invoice_number='')).count()


class Command(BaseCommand):
    help = 'Assign invoice numbers to bookings and event orders that have none.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Only count; write nothing.')

    def handle(self, *args, dry_run=False, **options):
        venue, event = _missing_count(Booking), _missing_count(EventOrder)
        self.stdout.write(f'Without an invoice number: {venue} venue booking(s), {event} event order(s).')
        if dry_run or not (venue or event):
            return
        numbered = assign_missing_invoice_numbers()
        self.stdout.write(self.style.SUCCESS(f'Numbered {numbered} row(s).'))
