"""
Listing map pins (location-based discovery).

Adds nullable latitude/longitude columns and backfills them from each
listing's OWN stored maps link — the exact pin the vendor marked, parsed with
the same rule the venue page's map embed already uses (venues/geo.py).

Safe by construction:
  * no network calls — an unresolved short link simply stays null;
  * reads `record`, writes ONLY the two new columns (record, address, status
    and bookings are untouched);
  * reversible: the reverse step is a no-op and the columns drop with the
    schema operations.

Unit siblings ("— Pitch 2") without a pin of their own take their base
venue's, as Listing.sync_coordinates does on every later save.
"""
from django.db import migrations, models


def backfill_coordinates(apps, schema_editor):
    from venues.geo import record_pin

    Listing = apps.get_model('venues', 'Listing')
    pins = {}
    units = []
    for listing in Listing.objects.only('id', 'record').iterator():
        point = record_pin(listing.record)
        if point is not None:
            pins[str(listing.id)] = point
            continue
        detail = (listing.record or {}).get('detail') or {}
        base_id = str(detail.get('unitOf') or '').strip() if isinstance(detail, dict) else ''
        if base_id:
            units.append((str(listing.id), base_id))
    for listing_id, base_id in units:
        if base_id in pins and listing_id not in pins:
            pins[listing_id] = pins[base_id]
    for listing_id, (lat, lng) in pins.items():
        Listing.objects.filter(pk=listing_id).update(latitude=lat, longitude=lng)


class Migration(migrations.Migration):

    dependencies = [
        ('venues', '0008_listing_deletion_requested_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='listing',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name='listing',
            index=models.Index(fields=['latitude', 'longitude'], name='venues_list_latitud_2a74f6_idx'),
        ),
        migrations.RunPython(backfill_coordinates, migrations.RunPython.noop),
    ]
