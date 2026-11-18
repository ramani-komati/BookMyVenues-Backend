"""
Resume / finish campaign sending outside a web request.

    python manage.py send_email_campaigns

Processes every queued or sending campaign whose worker is gone (lease
expired). Safe to run any time and as often as you like — a campaign with a
live worker is skipped, and nothing is ever sent twice (campaigns/sender.py).
On Render, a Cron Job running this every 10 minutes makes recovery after a
restart unattended; without one, opening the Email Campaigns page resumes it.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from campaigns.models import Campaign
from campaigns.sender import delivery_mode, process


class Command(BaseCommand):
    help = 'Send or resume queued promotional email campaigns.'

    def handle(self, *args, **options):
        if delivery_mode() == 'off':
            self.stdout.write('CAMPAIGN_DELIVERY is off — nothing to do.')
            return
        ids = list(Campaign.objects.filter(
            status__in=(Campaign.Status.QUEUED, Campaign.Status.SENDING),
        ).filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=timezone.now()),
        ).values_list('pk', flat=True))
        if not ids:
            self.stdout.write('No campaigns waiting.')
        for campaign_id in ids:
            outcome = process(campaign_id)
            self.stdout.write(f'{campaign_id}: {outcome}')
