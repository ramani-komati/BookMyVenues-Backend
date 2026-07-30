"""
Backfill the new is_customer flag:
- every PUBLIC-role account signed up through the customer app -> customer
- anyone who has ever made a booking (vendors/admins included) -> customer
"""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    Booking = apps.get_model('bookings', 'Booking')

    User.objects.filter(role='PUBLIC').update(is_customer=True)
    booker_ids = (
        Booking.objects.exclude(user=None).values_list('user_id', flat=True).distinct()
    )
    User.objects.filter(id__in=list(booker_ids)).update(is_customer=True)


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0005_user_is_customer'),
        ('bookings', '0005_booking_discount_amount_booking_offer'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
