"""
Public campaign endpoints (outside the admin panel).

    GET/PATCH /api/users/me/email-preferences   the customer's OWN opt-in (JWT)
    GET/POST  /api/email/unsubscribe?token=…    the link in every campaign email
    POST      /api/email/webhooks/resend        delivery events from Resend
"""
import base64
import hashlib
import hmac
import json
import logging
import time

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User

from .models import (
    CampaignRecipient,
    EmailPreference,
    EmailSuppression,
    ResendWebhookEvent,
)
from .sender import read_unsubscribe_token

logger = logging.getLogger(__name__)


def _preference_json(user, pref):
    return {
        'marketingEmails': bool(pref and pref.marketing_opt_in),
        'hasEmail': bool(user.email),
        'updatedAt': pref.updated_at.isoformat() if pref else None,
    }


def opt_out(user_id, source, campaign_id=None):
    """Record an unsubscribe. Idempotent. Returns False for an unknown user."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return False
    now = timezone.now()
    pref, _ = EmailPreference.objects.get_or_create(user=user)
    if pref.marketing_opt_in or pref.unsubscribed_at is None:
        pref.marketing_opt_in = False
        pref.unsubscribed_at = now
        pref.source = source
        pref.save(update_fields=['marketing_opt_in', 'unsubscribed_at', 'source', 'updated_at'])
    if campaign_id:
        CampaignRecipient.objects.filter(
            campaign_id=campaign_id, user_id=user.pk, unsubscribed_at__isnull=True,
        ).update(unsubscribed_at=now)
    return True


class EmailPreferencesView(APIView):
    """The customer's own switch — the ONLY way consent is ever given."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        pref = EmailPreference.objects.filter(user=request.user).first()
        return Response({'preferences': _preference_json(request.user, pref)})

    def patch(self, request):
        data = request.data if isinstance(request.data, dict) else {}
        wanted = data.get('marketingEmails')
        if not isinstance(wanted, bool):
            return Response({'message': 'marketingEmails must be true or false.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if wanted and not request.user.email:
            return Response({'message': 'Add an email address to your profile first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        pref, _ = EmailPreference.objects.get_or_create(user=request.user)
        if wanted != pref.marketing_opt_in:
            now = timezone.now()
            pref.marketing_opt_in = wanted
            pref.source = EmailPreference.Source.ACCOUNT
            if wanted:
                pref.consented_at = now
            else:
                pref.unsubscribed_at = now
            pref.save()
        return Response({'preferences': _preference_json(request.user, pref)})


@method_decorator(csrf_exempt, name='dispatch')
class UnsubscribeView(View):
    """The unsubscribe link. GET only SHOWS a confirm button — mail scanners
    and link previews open every link, and must not unsubscribe anyone. The
    POST does it: from that button, or from a mail app's one-click button
    (RFC 8058: body "List-Unsubscribe=One-Click"). CSRF-exempt by necessity
    (mail apps post without a token); the signed token is the authority and
    the only thing it can do is switch promotional email OFF."""

    template = 'campaigns/unsubscribe.html'

    def _context(self, request, **extra):
        site = str(settings.BRAND_SITE_URL or '').rstrip('/')
        return {'site_url': site, 'account_url': f'{site}/account', **extra}

    def get(self, request):
        token = request.GET.get('token', '')
        payload = read_unsubscribe_token(token)
        if payload is None:
            return render(request, self.template, self._context(request, state='invalid'), status=400)
        if payload.get('t'):
            return render(request, self.template, self._context(request, state='test'))
        pref = EmailPreference.objects.filter(user_id=payload['u']).first()
        state = 'confirm' if pref is None or pref.marketing_opt_in else 'already'
        return render(request, self.template, self._context(request, state=state, token=token))

    def post(self, request):
        token = request.GET.get('token') or request.POST.get('token') or ''
        payload = read_unsubscribe_token(token)
        one_click = request.POST.get('List-Unsubscribe') == 'One-Click'
        if payload is None:
            if one_click:
                return HttpResponse('Invalid link.', status=400, content_type='text/plain')
            return render(request, self.template, self._context(request, state='invalid'), status=400)
        if not payload.get('t'):
            source = EmailPreference.Source.ONE_CLICK if one_click else EmailPreference.Source.UNSUBSCRIBE_LINK
            opt_out(payload['u'], source, payload.get('c'))
        if one_click:
            return HttpResponse('Unsubscribed.', content_type='text/plain')
        state = 'test' if payload.get('t') else 'done'
        return render(request, self.template, self._context(request, state=state))


# ---------------------------------------------------------------------------
# Resend webhook
# ---------------------------------------------------------------------------

WEBHOOK_TOLERANCE_SECONDS = 5 * 60
# Later states never fall back to earlier ones (a late "delivered" must not
# undo a bounce).
_RANK = {
    CampaignRecipient.Status.SENT: 1,
    CampaignRecipient.Status.DELIVERED: 2,
    CampaignRecipient.Status.BOUNCED: 3,
    CampaignRecipient.Status.FAILED: 3,
    CampaignRecipient.Status.COMPLAINED: 4,
}


def verify_svix(secret, headers, raw_body):
    """Resend signs webhooks the Svix way: HMAC-SHA256 over
    "<id>.<timestamp>.<body>" with the base64 secret after "whsec_"."""
    msg_id = headers.get('svix-id', '')
    stamp = headers.get('svix-timestamp', '')
    signatures = headers.get('svix-signature', '')
    if not (secret and msg_id and stamp and signatures):
        return False
    try:
        if abs(time.time() - int(stamp)) > WEBHOOK_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.split('_', 1)[1] if secret.startswith('whsec_') else secret)
    except (ValueError, TypeError):
        return False
    signed = f'{msg_id}.{stamp}.'.encode() + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in signatures.split():
        version, _, value = part.partition(',')
        if version == 'v1' and hmac.compare_digest(value, expected):
            return True
    return False


def _suppress(email, reason, detail=''):
    email = str(email or '').strip().lower()
    if email:
        EmailSuppression.objects.get_or_create(
            email=email, defaults={'reason': reason, 'detail': str(detail)[:300]})


def apply_event(event_type, data):
    """Apply one Resend event to the campaign recipient it is about.
    Events about other mail (OTP, bookings) match nothing and are ignored."""
    email_id = str((data or {}).get('email_id') or '')
    if not email_id:
        return False
    row = CampaignRecipient.objects.filter(resend_id=email_id).first()
    if row is None:
        return False
    now = timezone.now()
    fields = {}

    def advance(new_status):
        if _RANK.get(new_status, 0) > _RANK.get(row.status, 0):
            fields['status'] = new_status

    if event_type == 'email.delivered':
        advance(CampaignRecipient.Status.DELIVERED)
        fields['delivered_at'] = row.delivered_at or now
    elif event_type == 'email.bounced':
        bounce = (data or {}).get('bounce') or {}
        advance(CampaignRecipient.Status.BOUNCED)
        fields['bounced_at'] = row.bounced_at or now
        fields['error'] = str(bounce.get('message') or 'Bounced')[:300]
        # Only a permanent bounce retires the address; a full mailbox
        # ("Transient") may work next time.
        if 'transient' not in str(bounce.get('type') or '').lower():
            _suppress(row.email, EmailSuppression.Reason.BOUNCE, fields['error'])
    elif event_type == 'email.complained':
        advance(CampaignRecipient.Status.COMPLAINED)
        _suppress(row.email, EmailSuppression.Reason.COMPLAINT)
        if row.user_id:
            opt_out(row.user_id, EmailPreference.Source.COMPLAINT, row.campaign_id)
    elif event_type == 'email.failed':
        advance(CampaignRecipient.Status.FAILED)
        fields['error'] = str(((data or {}).get('failed') or {}).get('reason') or 'Delivery failed')[:300]
    elif event_type == 'email.opened':
        fields['opened_at'] = row.opened_at or now
    elif event_type == 'email.clicked':
        fields['clicked_at'] = row.clicked_at or now
        fields['opened_at'] = row.opened_at or now          # a click implies an open
        advance(CampaignRecipient.Status.DELIVERED)         # …and a delivery
        fields['delivered_at'] = row.delivered_at or now
    else:
        return False
    CampaignRecipient.objects.filter(pk=row.pk).update(**fields)
    return True


@method_decorator(csrf_exempt, name='dispatch')
class ResendWebhookView(View):

    def post(self, request):
        secret = getattr(settings, 'RESEND_WEBHOOK_SECRET', '')
        if not secret:
            return HttpResponse('Webhook not configured.', status=503, content_type='text/plain')
        if not verify_svix(secret, request.headers, request.body):
            return HttpResponse('Bad signature.', status=401, content_type='text/plain')
        try:
            event = json.loads(request.body.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return HttpResponse('Bad JSON.', status=400, content_type='text/plain')
        event_type = str(event.get('type') or '')
        try:
            with transaction.atomic():
                ResendWebhookEvent.objects.create(
                    svix_id=request.headers.get('svix-id', '')[:100], event_type=event_type[:40])
                apply_event(event_type, event.get('data') or {})
        except IntegrityError:
            pass                                             # already applied
        return HttpResponse('ok', content_type='text/plain')
