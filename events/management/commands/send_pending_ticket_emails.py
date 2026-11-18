"""
python manage.py send_pending_ticket_emails [--days 2] [--dry-run]

Retry the ticket confirmation (email with PDF tickets) for PAID orders whose
first send never got through — e.g. the email provider was down when the
payment was verified. Each order goes through the same once-only claim the
payment flow uses (events/payments._notify_confirmed), so running this twice,
or alongside a webhook redelivery, can never email a customer twice.

Only orders paid within the last --days (default 2) are considered, so old
orders from before emails were switched on are never suddenly emailed.
"""
import datetime

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from events.models import EventOrder


class Command(BaseCommand):
    help = 'Retry ticket confirmation emails for recently paid orders whose send failed.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=2)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, days, dry_run, **options):
        from events.payments import _notify_confirmed

        since = timezone.now() - datetime.timedelta(days=max(1, min(days, 30)))
        pending = EventOrder.objects.filter(
            status='confirmed', confirmation_sent_at__isnull=True,
        ).exclude(razorpay_payment_id='').filter(
            Q(paid_at__gte=since) | Q(paid_at__isnull=True, created_at__gte=since),
        )
        ids = list(pending.values_list('pk', flat=True))
        if dry_run:
            self.stdout.write(f'{len(ids)} order(s) would be retried: {", ".join(ids) or "none"}')
            return
        sent = 0
        for pk in ids:
            _notify_confirmed(pk)
            if EventOrder.objects.filter(pk=pk, confirmation_sent_at__isnull=False).exists():
                sent += 1
        self.stdout.write(f'Retried {len(ids)} order(s); {sent} confirmation(s) now sent.')
