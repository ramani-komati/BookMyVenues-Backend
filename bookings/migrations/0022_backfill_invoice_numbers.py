"""
Give every EXISTING venue booking and event ticket order its invoice number.

Rows are numbered oldest first, each in the financial year of its own
created_at (bookings/invoices.py). Only the new invoice_number column is
written: ids, payment references, statuses and amounts are untouched, and no
row is created or deleted. Rows that already have a number are skipped, so
re-running is harmless — `python manage.py assign_invoice_numbers` runs the
same backfill for any rows created by the old server during the deploy.

paid_at is deliberately NOT backfilled: the time an older payment succeeded
was never recorded, and it is not guessed from created_at.

Reversing leaves the numbers in place (they are dropped with the columns if
0021 / events 0006 are reversed too).
"""
from django.db import migrations


def number_existing_rows(apps, schema_editor):
    from bookings.invoices import assign_missing_invoice_numbers

    assign_missing_invoice_numbers(
        apps.get_model('bookings', 'Booking'),
        apps.get_model('events', 'EventOrder'),
        apps.get_model('bookings', 'InvoiceSequence'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0021_invoice_number_paid_at'),
        ('events', '0006_eventorder_invoice_number_paid_at'),
    ]

    operations = [
        migrations.RunPython(number_existing_rows, migrations.RunPython.noop),
    ]
