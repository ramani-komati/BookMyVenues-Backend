"""
Booking.fee defaults to 20 (correct for every existing online booking — they
were all charged ₹20), but walk-ins never carry a platform fee: set 0.
"""
from django.db import migrations


def walkin_fee_zero(apps, schema_editor):
    Booking = apps.get_model('bookings', 'Booking')
    Booking.objects.filter(walk_in=True).update(fee=0)


class Migration(migrations.Migration):
    dependencies = [
        ('bookings', '0006_booking_fee'),
    ]

    operations = [
        migrations.RunPython(walkin_fee_zero, migrations.RunPython.noop),
    ]
