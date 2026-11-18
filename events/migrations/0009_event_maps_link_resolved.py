from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: where an event's Google Maps share link redirects to, so the
    event gets the exact pin that short link points at. Existing rows get {}
    and keep their current latitude/longitude until next saved."""

    dependencies = [
        ('events', '0008_event_status_ended'),
    ]

    operations = [
        migrations.AddField(
            model_name='event',
            name='maps_link_resolved',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
