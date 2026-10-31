"""
python manage.py archive_ended_events — move LIVE events whose last show is
over to `ended` (events/lifecycle.py). Safe to run from cron at any
interval; nothing is deleted and orders are untouched.
"""
from django.core.management.base import BaseCommand

from events.lifecycle import archive_ended_events


class Command(BaseCommand):
    help = 'Archive (status "ended") live events whose last date is over.'

    def handle(self, *args, **options):
        moved = archive_ended_events()
        self.stdout.write(f'Archived {moved} ended event(s).')
