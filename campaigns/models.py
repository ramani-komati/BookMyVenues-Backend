"""
Promotional email campaigns — a stream kept apart from transactional mail.

Transactional email (OTP codes, booking and ticket confirmations) lives in
accounts/otp.py and bookings/notifications.py and is NOT touched by anything
here: it never reads a marketing preference, so unsubscribing from offers can
never stop a sign-in code or a booking receipt.

    EmailPreference     a customer's own marketing consent (opt-IN, default off)
    EmailSuppression    addresses that must never get promotional mail again
                        (hard bounce, spam complaint)
    Campaign            one promotional email: content + audience + send state
    CampaignRecipient   the frozen recipient list of a sent campaign, one row
                        per address, carrying its delivery status
    ResendWebhookEvent  webhook ids already applied (Resend retries deliveries)
"""
import uuid

from django.conf import settings
from django.db import models


class EmailPreference(models.Model):
    """Marketing consent, set ONLY by the customer (My account toggle) or by
    their own unsubscribe / spam complaint. A missing row means "never opted
    in" — no promotional email. Admins have no write path to this table."""

    class Source(models.TextChoices):
        ACCOUNT = 'account', 'My account'
        UNSUBSCRIBE_LINK = 'unsubscribe_link', 'Unsubscribe link'
        ONE_CLICK = 'one_click', 'One-click unsubscribe (mail app)'
        COMPLAINT = 'complaint', 'Marked as spam'

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='email_preference',
    )
    marketing_opt_in = models.BooleanField(default=False)
    consented_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'Email preference of {self.user_id}: {"in" if self.marketing_opt_in else "out"}'


class EmailSuppression(models.Model):
    """An address promotional mail must skip, whatever its owner's toggle
    says — mailing a dead or complaining address hurts the sending domain's
    reputation, which would also slow down OTP and booking mail."""

    class Reason(models.TextChoices):
        BOUNCE = 'bounce', 'Hard bounce'
        COMPLAINT = 'complaint', 'Spam complaint'

    email = models.CharField(max_length=254, unique=True)   # stored lower-case
    reason = models.CharField(max_length=12, choices=Reason.choices)
    detail = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Suppressed ({self.reason})'


class Campaign(models.Model):

    class Status(models.TextChoices):
        DRAFT = 'draft', 'Draft'
        QUEUED = 'queued', 'Queued'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        PARTIAL = 'partial', 'Sent with failures'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    subject = models.CharField(max_length=150, blank=True, default='')
    preview_text = models.CharField(max_length=200, blank=True, default='')
    heading = models.CharField(max_length=150, blank=True, default='')
    body = models.TextField(blank=True, default='')
    # [{url, alt, link, caption}] in display order — the first is the banner
    # at the top of the email, the rest follow the body text.
    images = models.JSONField(default=list, blank=True)
    cta_label = models.CharField(max_length=40, blank=True, default='')
    cta_url = models.CharField(max_length=500, blank=True, default='')
    # {segment, state, city, category, days} — see campaigns/audience.py.
    audience = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # Bumped on every edit; a save carrying an older version is refused, so
    # two admins editing one draft never silently overwrite each other.
    version = models.PositiveIntegerField(default=1)
    # 'dry-run' sends go through the whole pipeline without calling Resend.
    dry_run = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # The public API origin unsubscribe links point at, frozen at send time.
    link_base = models.CharField(max_length=300, blank=True, default='')
    last_error = models.CharField(max_length=300, blank=True, default='')

    # Worker lease: only the holder of an unexpired lease may send. A worker
    # that dies simply lets it lapse and the next one resumes the campaign.
    lease_token = models.CharField(max_length=40, blank=True, default='')
    lease_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['status'])]

    def __str__(self):
        return self.name


class CampaignRecipient(models.Model):

    class Status(models.TextChoices):
        PENDING = 'pending', 'Waiting'
        SENDING = 'sending', 'Sending'
        SENT = 'sent', 'Sent'
        DELIVERED = 'delivered', 'Delivered'
        BOUNCED = 'bounced', 'Bounced'
        COMPLAINED = 'complained', 'Marked as spam'
        FAILED = 'failed', 'Failed'
        SKIPPED = 'skipped', 'Skipped'

    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='recipients')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='+',
    )
    # Frozen at send time (lower-case), so a retry renders byte-identical
    # mail and Resend's idempotency key matches.
    email = models.CharField(max_length=254)
    name = models.CharField(max_length=150, blank=True, default='')

    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    # Every row sent in one Resend batch call shares this; a retry of the same
    # batch reuses it as the Idempotency-Key, so it can never send twice.
    batch_key = models.CharField(max_length=64, blank=True, default='', db_index=True)
    resend_id = models.CharField(max_length=64, blank=True, default='', db_index=True)
    error = models.CharField(max_length=300, blank=True, default='')

    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    bounced_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']
        constraints = [
            # The duplicate-send guard at the bottom of the stack: one address
            # appears at most once per campaign.
            models.UniqueConstraint(fields=['campaign', 'email'], name='unique_campaign_recipient'),
        ]
        indexes = [models.Index(fields=['campaign', 'status'])]

    def __str__(self):
        return f'Recipient {self.pk} of {self.campaign_id}'


class ResendWebhookEvent(models.Model):
    """Resend (via Svix) retries a webhook until it gets a 2xx — remembering
    the ids already applied makes every delivery idempotent."""

    svix_id = models.CharField(max_length=100, unique=True)
    event_type = models.CharField(max_length=40, blank=True, default='')
    received_at = models.DateTimeField(auto_now_add=True)
