"""
One 6-character booking ID across ALL booking types.

Creates BookingCode — the registry whose primary key makes a 6-character
booking ID unique across venue bookings AND event orders — and registers every
6-character ID that already exists (today: event ticket orders only; venue
bookings so far all have 'bk_…' IDs).

COLLISION CHECK FIRST: if the same 6-character ID somehow exists in both
tables, the migration stops with the offending IDs instead of guessing which
booking owns it. Nothing is renamed, re-keyed or deleted: existing bookings,
payments and their 'bk_…' / 'eo_…' IDs are untouched. Reversible (drops the
registry table only).
"""
import re

from django.db import migrations, models

SHORT = re.compile(r'^[A-Z0-9]{6}$')


def register_existing_codes(apps, schema_editor):
    Booking = apps.get_model('bookings', 'Booking')
    EventOrder = apps.get_model('events', 'EventOrder')
    BookingCode = apps.get_model('bookings', 'BookingCode')

    venue = {pk for pk in Booking.objects.values_list('pk', flat=True) if SHORT.match(pk)}
    event = {pk for pk in EventOrder.objects.values_list('pk', flat=True) if SHORT.match(pk)}
    clashes = sorted(venue & event)
    if clashes:
        raise RuntimeError(
            'The same 6-character booking ID exists as both a venue booking and '
            f'an event order: {", ".join(clashes)}. Resolve these by hand before '
            'migrating — nothing has been changed.'
        )
    BookingCode.objects.bulk_create(
        [BookingCode(code=code, kind='venue') for code in sorted(venue)]
        + [BookingCode(code=code, kind='event') for code in sorted(event)]
    )


class Migration(migrations.Migration):

    dependencies = [
        ('bookings', '0017_booking_checkin'),
        ('events', '0004_event_coordinates_order_checkin'),
    ]

    operations = [
        migrations.CreateModel(
            name='BookingCode',
            fields=[
                ('code', models.CharField(max_length=6, primary_key=True, serialize=False)),
                ('kind', models.CharField(max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.RunPython(register_existing_codes, migrations.RunPython.noop),
    ]
