"""
Event map pins + ticket check-in.

* Event.latitude / longitude — nullable, backfilled from each event's OWN
  maps_link (exact pin only, venues/geo.py), else from the listed venue it is
  held at. No network calls; writes only the two new columns.
* EventOrder.checked_in_at / checked_in_by — nullable, no backfill: nothing
  has been checked in before this feature existed.
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def backfill_coordinates(apps, schema_editor):
    from venues.geo import pin_from_maps_link

    Event = apps.get_model('events', 'Event')
    Listing = apps.get_model('venues', 'Listing')
    for event in Event.objects.only('id', 'maps_link', 'listing_id').iterator():
        point = pin_from_maps_link(event.maps_link)
        if point is None and event.listing_id:
            venue = (
                Listing.objects.filter(pk=event.listing_id)
                .values_list('latitude', 'longitude')
                .first()
            )
            if venue and venue[0] is not None and venue[1] is not None:
                point = venue
        if point is not None:
            Event.objects.filter(pk=event.pk).update(latitude=point[0], longitude=point[1])


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0003_event_featured'),
        # Venue pins are backfilled first, so an event held at a listed venue
        # can inherit that venue's pin.
        ('venues', '0009_listing_coordinates'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='latitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='event',
            name='longitude',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='eventorder',
            name='checked_in_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='eventorder',
            name='checked_in_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='checked_in_event_orders', to=settings.AUTH_USER_MODEL),
        ),
        migrations.RunPython(backfill_coordinates, migrations.RunPython.noop),
    ]
