"""
python manage.py review_test_data — READ-ONLY report of venues, events and
event orders that look like test data (adminpanel/data_review.py). Changes
nothing; confirmed test venues/events are paused from the super admin panel.
"""
from django.core.management.base import BaseCommand

from adminpanel.data_review import report


class Command(BaseCommand):
    help = 'List records that look like test data (read-only).'

    def handle(self, *args, **options):
        data = report()
        for title, key, label in (('Venues', 'venues', 'name'), ('Events', 'events', 'name'),
                                  ('Event orders', 'orders', 'eventName')):
            rows = data[key]
            self.stdout.write(f'{title}: {len(rows)} flagged')
            for row in rows:
                self.stdout.write(f"  [{row['status']}] {row[label]} ({row['id']}) — {'; '.join(row['reasons'])}")
        self.stdout.write('Read-only — nothing was changed.')
