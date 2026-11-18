"""python manage.py release_expired_holds [--dry-run]

Tidies 5-minute checkout holds that lapsed before the customer ever started
paying (bookings/holds.release_expired_holds). Optional: availability already
treats every expired hold as free the moment it lapses, so nothing waits for
this. Safe to run on a schedule (e.g. every 10 minutes).
"""
from django.core.management.base import BaseCommand

from bookings import holds
from bookings.models import Booking


class Command(BaseCommand):
    help = 'Cancel expired 5-minute slot holds that never reached the payment gateway.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, dry_run=False, **options):
        if dry_run:
            count = Booking.objects.filter(holds.expired_hold_q(), razorpay_order_id='').count()
            self.stdout.write(f'{count} expired hold(s) would be released.')
            return
        count = holds.release_expired_holds()
        self.stdout.write(f'Released {count} expired hold(s).')
