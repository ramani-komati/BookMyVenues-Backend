"""
Sending a campaign: freeze the recipient list, then deliver it in batches in
the background, safely resumable at any point.

DELIVERY MODE — settings.CAMPAIGN_DELIVERY, default 'off':
  off       nothing can be sent (tests, local development, a fresh deploy)
  dry-run   the full pipeline runs but Resend is never called — for
            rehearsing locally; the campaign is marked as a dry run
  resend    real email through Resend

DUPLICATE-SEND GUARDS, outermost first:
  1. queue() flips draft -> queued under a row lock; a second click, a second
     admin or a replayed request finds it no longer a draft and is refused.
  2. The recipient list is frozen once, unique per (campaign, address).
  3. Only the holder of the campaign's lease sends; a worker that dies lets
     the lease lapse and the next one RESUMES from the frozen list.
  4. Every Resend call carries an Idempotency-Key derived from the batch, and
     a retried batch re-renders byte-identical mail from frozen values — so
     even a request whose answer was lost cannot email anyone twice.

BACKGROUND WORK without a task queue (the backend has none): queue() starts a
daemon thread after the transaction commits. If the server restarts mid-send,
the campaign is resumed by the next admin visit to the Campaigns page, or by
`manage.py send_email_campaigns` (run it from a cron job for unattended
recovery). All progress lives in the database, never in the thread.
"""
import datetime
import logging
import threading
import time
import uuid

from django.conf import settings
from django.core import signing
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.db.models.functions import Coalesce, Lower
from django.utils import timezone

from . import content, resend_client
from .audience import segment_queryset
from .models import Campaign, CampaignRecipient, EmailPreference, EmailSuppression

logger = logging.getLogger(__name__)

BATCH_SIZE = resend_client.BATCH_LIMIT
LEASE = datetime.timedelta(minutes=5)
# Resend's default limit is 2 requests per second per team, SHARED with the
# OTP / booking mail. One batch call a second (= up to 100 emails/second)
# leaves the other half for transactional mail, so a big campaign never
# starves a sign-in code or a booking confirmation.
REQUEST_GAP_SECONDS = 1.0
QUICK_RETRIES = (2, 6)                    # in-call retries for a transient error
RETRY_DELAYS = (60, 300, 1800)            # then re-queued with these waits
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
MAX_TEST_RECIPIENTS = 5

UNSUBSCRIBE_SALT = 'campaigns.unsubscribe'
UNSUBSCRIBE_PATH = '/api/email/unsubscribe'


class DeliveryOff(Exception):
    pass


def delivery_mode():
    mode = str(getattr(settings, 'CAMPAIGN_DELIVERY', 'off') or 'off').strip().lower()
    return mode if mode in ('off', 'dry-run', 'resend') else 'off'


def from_address():
    return getattr(settings, 'CAMPAIGN_FROM', '') or settings.RESEND_FROM


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------

def _signer():
    return signing.Signer(salt=UNSUBSCRIBE_SALT, algorithm='sha256')


def unsubscribe_token(user_id, campaign_id=None, *, test=False):
    """Signed, never-expiring and DETERMINISTIC (no timestamp): a retried
    batch must render the identical link. Carries ids only — no address."""
    payload = {'u': user_id}
    if campaign_id:
        payload['c'] = str(campaign_id)
    if test:
        payload['t'] = 1
    return _signer().sign_object(payload)


def read_unsubscribe_token(token):
    try:
        payload = _signer().unsign_object(str(token or ''))
    except (signing.BadSignature, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) and 'u' in payload else None


def unsubscribe_url(base, token):
    return f'{base.rstrip("/")}{UNSUBSCRIBE_PATH}?token={token}'


def public_base(request=None):
    configured = str(getattr(settings, 'PUBLIC_API_BASE_URL', '') or '').strip()
    if configured:
        return configured.rstrip('/')
    if request is not None:
        return request.build_absolute_uri('/').rstrip('/')
    return ''


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------

def _payload(campaign, rendered, *, to, name, unsub_url, test=False):
    subject, html, text = content.personalize(rendered, name=name, unsubscribe_url=unsub_url)
    payload = {
        'from': from_address(),
        'to': [to],
        'subject': subject,
        'html': html,
        'text': text,
        # Lets mail apps show their own "Unsubscribe" button (RFC 2369) and
        # unsubscribe in one click without a browser (RFC 8058) — Gmail and
        # Yahoo require both for bulk senders.
        'headers': {
            'List-Unsubscribe': f'<{unsub_url}>',
            'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
        },
        # Separates this stream from transactional mail in Resend's logs.
        'tags': [
            {'name': 'stream', 'value': 'test' if test else 'marketing'},
            {'name': 'campaign', 'value': str(campaign.pk)},
        ],
    }
    reply_to = getattr(settings, 'CAMPAIGN_REPLY_TO', '')
    if reply_to:
        payload['reply_to'] = reply_to
    return payload


# ---------------------------------------------------------------------------
# Test email
# ---------------------------------------------------------------------------

def clean_test_recipients(raw, fallback=''):
    items = raw if isinstance(raw, list) else str(raw or '').replace(';', ',').split(',')
    emails = []
    for item in items:
        email = str(item or '').strip().lower()
        if email and email not in emails:
            emails.append(email)
    if not emails and fallback:
        emails = [fallback.lower()]
    if not emails:
        raise ValueError('Enter at least one email address for the test.')
    if len(emails) > MAX_TEST_RECIPIENTS:
        raise ValueError(f'Send a test to at most {MAX_TEST_RECIPIENTS} addresses.')
    for email in emails:
        try:
            validate_email(email)
        except ValidationError:
            raise ValueError(f'"{email}" is not a valid email address.')
    return emails


def send_test(campaign, emails, *, admin, base):
    """Send the campaign as it is now to a few addresses the admin typed.
    Not recorded as a campaign send and never touches its recipient list."""
    mode = delivery_mode()
    if mode == 'off':
        raise DeliveryOff()
    rendered = content.render_template(campaign, is_test=True)
    token = unsubscribe_token(admin.pk, campaign.pk, test=True)
    name = getattr(admin, 'name', '') or ''
    results = []
    for email in emails:
        payload = _payload(campaign, rendered, to=email, name=name,
                           unsub_url=unsubscribe_url(base, token), test=True)
        if mode == 'dry-run':
            results.append({'email': email, 'id': 'dry-run'})
            continue
        resend_id = resend_client.send_one(payload, idempotency_key=f'bmv-test/{uuid.uuid4().hex}')
        results.append({'email': email, 'id': resend_id})
    return results


# ---------------------------------------------------------------------------
# Queueing
# ---------------------------------------------------------------------------

class QueueRefused(Exception):
    def __init__(self, message, code, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


def queue(campaign_id, *, admin, expected_count, version, base):
    """Freeze the audience and hand the campaign to a worker. Raises
    QueueRefused for every reason it must not go out."""
    mode = delivery_mode()
    if mode == 'off':
        raise QueueRefused(
            'Sending is switched off on this server (CAMPAIGN_DELIVERY=off). '
            'Nothing was sent.', 'DELIVERY_OFF')
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.status != Campaign.Status.DRAFT:
            raise QueueRefused('This campaign has already been sent or queued.', 'ALREADY_SENT')
        if version is not None and int(version) != campaign.version:
            raise QueueRefused('Someone changed this campaign since you opened it. '
                               'Reload it before sending.', 'STALE')
        problems = content.send_problems(campaign)
        if problems:
            raise QueueRefused(next(iter(problems.values())), 'INCOMPLETE', problems=problems)

        rows = (segment_queryset(campaign.audience).order_by('pk')
                .values_list('pk', 'email_lower', 'name'))
        recipients, seen = [], set()
        for user_id, email, name in rows.iterator(chunk_size=2000):
            if email in seen:
                continue
            seen.add(email)
            recipients.append(CampaignRecipient(
                campaign=campaign, user_id=user_id, email=email, name=name or ''))
        if not recipients:
            raise QueueRefused('No customers match this audience right now.', 'NO_RECIPIENTS')
        if expected_count is None or int(expected_count) != len(recipients):
            raise QueueRefused(
                f'The audience is now {len(recipients):,} customers (you confirmed '
                f'{expected_count}). Check the new count and confirm again.',
                'COUNT_CHANGED', count=len(recipients))

        CampaignRecipient.objects.bulk_create(recipients, batch_size=1000, ignore_conflicts=True)
        now = timezone.now()
        campaign.status = Campaign.Status.QUEUED
        campaign.queued_at = now
        campaign.sent_by = admin
        campaign.dry_run = mode == 'dry-run'
        campaign.link_base = base
        campaign.last_error = ''
        campaign.save(update_fields=['status', 'queued_at', 'sent_by', 'dry_run',
                                     'link_base', 'last_error', 'updated_at'])
        transaction.on_commit(lambda: start_worker(campaign.pk))
    return campaign


def cancel(campaign_id):
    """Stop a queued / sending campaign. Mail already handed to Resend stays
    sent; everything still waiting is marked skipped."""
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        if campaign.status not in (Campaign.Status.QUEUED, Campaign.Status.SENDING):
            return None
        now = timezone.now()
        campaign.status = Campaign.Status.CANCELLED
        campaign.cancelled_at = now
        campaign.finished_at = now
        campaign.lease_token = ''
        campaign.lease_expires_at = None
        campaign.save(update_fields=['status', 'cancelled_at', 'finished_at',
                                     'lease_token', 'lease_expires_at', 'updated_at'])
        # Rows mid-call ('sending') are left as they are: the call may have
        # gone through, and calling them skipped could be a lie.
        campaign.recipients.filter(status=CampaignRecipient.Status.PENDING).update(
            status=CampaignRecipient.Status.SKIPPED, error='Campaign cancelled.')
    return campaign


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

def start_worker(campaign_id):
    """Run process() on a daemon thread (off in tests: they call it directly)."""
    if not getattr(settings, 'CAMPAIGN_INLINE_WORKER', True):
        return

    def run():
        close_old_connections()
        try:
            process(campaign_id)
        except Exception:
            logger.exception('Campaign worker crashed; it will be resumed.')
        finally:
            connection.close()

    threading.Thread(target=run, name=f'campaign-{campaign_id}', daemon=True).start()


def resume_stalled():
    """Restart campaigns whose worker vanished (server restart, crash).
    Cheap and safe to call often: process() takes the lease atomically, so a
    campaign that still has a live worker is simply left alone."""
    now = timezone.now()
    stalled = Campaign.objects.filter(
        status__in=(Campaign.Status.QUEUED, Campaign.Status.SENDING),
    ).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now))
    ids = list(stalled.values_list('pk', flat=True))
    for campaign_id in ids:
        start_worker(campaign_id)
    return ids


def _acquire(campaign_id, token):
    now = timezone.now()
    return Campaign.objects.filter(
        pk=campaign_id, status__in=(Campaign.Status.QUEUED, Campaign.Status.SENDING),
    ).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lt=now)).update(
        status=Campaign.Status.SENDING, lease_token=token, lease_expires_at=now + LEASE,
        started_at=Coalesce('started_at', now),
    ) == 1


def _renew(campaign_id, token):
    """False once the lease is lost — cancelled, or taken over."""
    return Campaign.objects.filter(
        pk=campaign_id, lease_token=token, status=Campaign.Status.SENDING,
    ).update(lease_expires_at=timezone.now() + LEASE) == 1


def _release(campaign_id, token):
    Campaign.objects.filter(pk=campaign_id, lease_token=token).update(
        lease_token='', lease_expires_at=None)


def _claim(campaign):
    """The next batch: first any batch a dead worker left mid-call (resent
    under its ORIGINAL key), then fresh rows that are due."""
    leftover = (campaign.recipients.filter(status=CampaignRecipient.Status.SENDING)
                .exclude(batch_key='').values_list('batch_key', flat=True).first())
    if leftover:
        return leftover, list(campaign.recipients.filter(
            status=CampaignRecipient.Status.SENDING, batch_key=leftover))

    now = timezone.now()
    due = list(campaign.recipients.filter(status=CampaignRecipient.Status.PENDING).filter(
        Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now),
    ).order_by('pk')[:BATCH_SIZE])
    if not due:
        return None, []
    due = _drop_unsubscribed(due)
    if not due:
        return '', []
    key = f'{campaign.pk.hex}-{uuid.uuid4().hex[:16]}'
    CampaignRecipient.objects.filter(pk__in=[r.pk for r in due]).update(
        status=CampaignRecipient.Status.SENDING, batch_key=key, next_attempt_at=None)
    for row in due:
        row.status, row.batch_key = CampaignRecipient.Status.SENDING, key
    return key, due


def _drop_unsubscribed(rows):
    """Consent is re-checked right before sending: someone who unsubscribed
    (or bounced) after the campaign was queued is skipped, not mailed."""
    user_ids = [r.user_id for r in rows if r.user_id]
    still_in = set(EmailPreference.objects.filter(
        user_id__in=user_ids, marketing_opt_in=True).values_list('user_id', flat=True))
    suppressed = set(EmailSuppression.objects.annotate(e=Lower('email')).filter(
        e__in=[r.email for r in rows]).values_list('e', flat=True))
    keep, skip = [], []
    for row in rows:
        (keep if row.user_id in still_in and row.email not in suppressed else skip).append(row)
    if skip:
        CampaignRecipient.objects.filter(pk__in=[r.pk for r in skip]).update(
            status=CampaignRecipient.Status.SKIPPED,
            error='Unsubscribed or suppressed before sending.')
    return keep


def _call_with_quick_retries(func, *args):
    for attempt, wait in enumerate((*QUICK_RETRIES, None)):
        try:
            return func(*args)
        except resend_client.TransientError as error:
            if wait is None:
                raise
            delay = error.retry_after if error.retry_after else wait
            time.sleep(min(delay, 30))


def _mark_sent(rows, ids):
    now = timezone.now()
    for row, resend_id in zip(rows, ids):
        CampaignRecipient.objects.filter(pk=row.pk).update(
            status=CampaignRecipient.Status.SENT, resend_id=resend_id[:64],
            sent_at=now, attempts=row.attempts + 1, error='')


def _mark_failed(rows, message):
    CampaignRecipient.objects.filter(pk__in=[r.pk for r in rows]).update(
        status=CampaignRecipient.Status.FAILED, error=message[:300])


def _defer(rows, message):
    """Back to the queue with a growing wait, or failed after the last try."""
    now = timezone.now()
    for row in rows:
        attempts = row.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            CampaignRecipient.objects.filter(pk=row.pk).update(
                status=CampaignRecipient.Status.FAILED, attempts=attempts, error=message[:300])
            continue
        CampaignRecipient.objects.filter(pk=row.pk).update(
            status=CampaignRecipient.Status.PENDING, attempts=attempts, batch_key='',
            next_attempt_at=now + datetime.timedelta(seconds=RETRY_DELAYS[attempts - 1]),
            error=message[:300])


def _deliver(campaign, key, rows, rendered, dry_run):
    payloads = []
    for row in rows:
        token = unsubscribe_token(row.user_id, campaign.pk)
        payloads.append(_payload(campaign, rendered, to=row.email, name=row.name,
                                 unsub_url=unsubscribe_url(campaign.link_base, token)))
    if dry_run:
        _mark_sent(rows, [f'dry-run-{uuid.uuid4().hex[:20]}' for _ in rows])
        return
    try:
        ids = _call_with_quick_retries(resend_client.send_batch, payloads, f'bmv-campaign/{key}')
    except resend_client.TransientError as error:
        logger.warning('Campaign %s batch deferred: %s', campaign.pk, error)
        Campaign.objects.filter(pk=campaign.pk).update(last_error=str(error)[:300])
        _defer(rows, str(error))
        return
    except resend_client.PermanentError as error:
        # A 4xx means NOTHING in the batch was sent. Usually one bad address
        # spoils it; send one by one so only that row fails.
        logger.warning('Campaign %s batch refused, isolating: %s', campaign.pk, error)
        if len(rows) == 1:
            _mark_failed(rows, str(error))
            return
        for row, payload in zip(rows, payloads):
            try:
                resend_id = _call_with_quick_retries(
                    resend_client.send_one, payload, f'bmv-campaign/{key}/{row.pk}')
            except resend_client.TransientError as single_error:
                _defer([row], str(single_error))
            except resend_client.PermanentError as single_error:
                _mark_failed([row], str(single_error))
            else:
                _mark_sent([row], [resend_id])
            time.sleep(REQUEST_GAP_SECONDS)
        return
    _mark_sent(rows, ids)


def _finish(campaign_id):
    campaign = Campaign.objects.get(pk=campaign_id)
    rows = campaign.recipients
    delivered_like = rows.exclude(status__in=(
        CampaignRecipient.Status.FAILED, CampaignRecipient.Status.SKIPPED,
        CampaignRecipient.Status.PENDING, CampaignRecipient.Status.SENDING))
    failed = rows.filter(status=CampaignRecipient.Status.FAILED).exists()
    if not failed:
        status = Campaign.Status.SENT
    elif delivered_like.exists():
        status = Campaign.Status.PARTIAL
    else:
        status = Campaign.Status.FAILED
    Campaign.objects.filter(pk=campaign_id, status=Campaign.Status.SENDING).update(
        status=status, finished_at=timezone.now(), lease_token='', lease_expires_at=None)


def process(campaign_id, *, max_wait_seconds=600, sleep=time.sleep):
    """Deliver everything due for one campaign. Returns why it stopped:
    'done' | 'busy' (another worker holds it) | 'stopped' (cancelled) |
    'waiting' (only delayed retries remain — resumed later)."""
    token = uuid.uuid4().hex
    if not _acquire(campaign_id, token):
        return 'busy'
    waited = 0.0
    try:
        campaign = Campaign.objects.get(pk=campaign_id)
        rendered = content.render_template(campaign)
        while True:
            if not _renew(campaign_id, token):
                return 'stopped'
            key, rows = _claim(campaign)
            if key == '':
                continue                       # whole batch was skipped
            if rows:
                _deliver(campaign, key, rows, rendered, campaign.dry_run)
                if not campaign.dry_run:
                    sleep(REQUEST_GAP_SECONDS)
                continue
            next_due = (campaign.recipients.filter(status=CampaignRecipient.Status.PENDING)
                        .order_by('next_attempt_at').values_list('next_attempt_at', flat=True).first())
            if next_due is None:
                _finish(campaign_id)
                return 'done'
            wait = max(1.0, (next_due - timezone.now()).total_seconds())
            if waited + wait > max_wait_seconds:
                return 'waiting'
            nap = min(wait, 30.0)
            sleep(nap)
            waited += nap
    finally:
        _release(campaign_id, token)
