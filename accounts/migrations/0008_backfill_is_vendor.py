"""
Backfill is_vendor for every existing VENDOR-role account, so vendor access
is carried by the flag from here on (and survives a later role change).
"""
from django.db import migrations


def backfill(apps, schema_editor):
    User = apps.get_model('accounts', 'User')
    User.objects.filter(role='VENDOR').update(is_vendor=True)


class Migration(migrations.Migration):
    dependencies = [
        ('accounts', '0007_user_is_vendor'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
