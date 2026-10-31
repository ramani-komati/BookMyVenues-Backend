"""
Super-admin Email Campaigns API — /api/admin/campaigns…

Same auth and conventions as every other admin route (adminpanel/auth.py):
cookie session, ADMIN role only, JSON-only writes, {"detail"} errors, and a
server-written audit row for every action. Customer addresses are never sent
to the panel in full — counts, and masked addresses in delivery reports.

    GET    campaigns                    list + delivery stats (history)
    POST   campaigns                    create a draft
    GET    campaigns/options            segments, states/cities, categories, delivery mode
    POST   campaigns/audience           {audience} -> estimated recipient count
    POST   campaigns/audience/recipients {audience, page, search} -> the eligible
                                        customers (name, email, city/state,
                                        interests, why they qualify) — ADMIN only
    POST   campaigns/preview            {fields} -> rendered HTML (unsaved edits)
    GET    campaigns/images             image library (past campaign + banner art)
    POST   campaigns/images             upload one image (multipart)
    GET    campaigns/<id>               one campaign + stats
    PATCH  campaigns/<id>               edit a draft ({version} required)
    DELETE campaigns/<id>               delete a draft
    POST   campaigns/<id>/duplicate     copy into a new draft
    POST   campaigns/<id>/test          {to: [emails]} -> test email
    POST   campaigns/<id>/send          {version, expectedCount, confirm, confirmAll?}
    POST   campaigns/<id>/cancel        stop a queued / sending campaign
    GET    campaigns/<id>/recipients    delivery status per recipient (masked)
"""
import logging
import uuid

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from adminpanel.auth import CsrfExemptSessionAuthentication, IsAdmin, detail

from . import audience as audience_mod
from . import content, resend_client, sender
from .models import Campaign, CampaignRecipient

logger = logging.getLogger(__name__)

R = CampaignRecipient.Status
EDITABLE = (Campaign.Status.DRAFT,)


def _audit(request, action, campaign, change=''):
    from adminpanel.views import record_audit
    record_audit(request, action, campaign.name if campaign else 'Email campaign', change,
                 target_id=str(campaign.pk) if campaign else '')


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

_STAT_FILTERS = {
    'pending': Q(recipients__status__in=(R.PENDING, R.SENDING)),
    'sent': Q(recipients__status__in=(R.SENT, R.DELIVERED, R.BOUNCED, R.COMPLAINED)),
    'delivered': Q(recipients__status__in=(R.DELIVERED, R.COMPLAINED)),
    'bounced': Q(recipients__status=R.BOUNCED),
    'complained': Q(recipients__status=R.COMPLAINED),
    'failed': Q(recipients__status=R.FAILED),
    'skipped': Q(recipients__status=R.SKIPPED),
    'opened': Q(recipients__opened_at__isnull=False),
    'clicked': Q(recipients__clicked_at__isnull=False),
    'unsubscribed': Q(recipients__unsubscribed_at__isnull=False),
}


def with_stats(qs):
    return qs.annotate(
        stat_recipients=Count('recipients'),
        **{f'stat_{key}': Count('recipients', filter=f) for key, f in _STAT_FILTERS.items()},
    )


def _iso(value):
    return value.isoformat() if value else None


def _who(user):
    return (getattr(user, 'name', '') or getattr(user, 'email', '') or '') if user else ''


def campaign_row(campaign):
    stats = {'recipients': getattr(campaign, 'stat_recipients', 0)}
    for key in _STAT_FILTERS:
        stats[key] = getattr(campaign, f'stat_{key}', 0)
    return {
        'id': str(campaign.pk),
        'name': campaign.name,
        'subject': campaign.subject,
        'previewText': campaign.preview_text,
        'heading': campaign.heading,
        'body': campaign.body,
        'images': campaign.images or [],
        'ctaLabel': campaign.cta_label,
        'ctaUrl': campaign.cta_url,
        'audience': campaign.audience or {'segment': 'all'},
        'audienceLabel': audience_mod.describe(campaign.audience),
        'status': campaign.status,
        'statusLabel': campaign.get_status_display(),
        'editable': campaign.status in EDITABLE,
        'version': campaign.version,
        'dryRun': campaign.dry_run,
        'createdAt': _iso(campaign.created_at),
        'updatedAt': _iso(campaign.updated_at),
        'queuedAt': _iso(campaign.queued_at),
        'startedAt': _iso(campaign.started_at),
        'finishedAt': _iso(campaign.finished_at),
        'createdBy': _who(campaign.created_by),
        'sentBy': _who(campaign.sent_by),
        'lastError': campaign.last_error,
        'stats': stats,
        'problems': content.send_problems(campaign),
    }


def _load(campaign_id):
    return (with_stats(Campaign.objects.select_related('created_by', 'sent_by'))
            .filter(pk=campaign_id).first())


def _content_error(error):
    return Response({'detail': str(error), 'field': getattr(error, 'field', '')},
                    status=status.HTTP_400_BAD_REQUEST)


class _View(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    # JSON only — the same CSRF reasoning as adminpanel.views._AdminWriteView.
    parser_classes = [JSONParser]

    def _body(self, request):
        return request.data if isinstance(request.data, dict) else {}


def _apply(campaign, body):
    """Content + audience from a request body. Raises ContentError/AudienceError."""
    content.apply_fields(campaign, body)
    if 'audience' in body:
        campaign.audience = audience_mod.clean_audience(body.get('audience'))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

class CampaignsView(_View):

    def get(self, request):
        # Any campaign whose worker vanished (restart / crash) is picked up
        # again simply by an admin looking at the list.
        sender.resume_stalled()
        rows = with_stats(Campaign.objects.select_related('created_by', 'sent_by'))
        return Response({
            'campaigns': [campaign_row(c) for c in rows],
            'delivery': _delivery_info(),
        })

    def post(self, request):
        body = self._body(request)
        campaign = Campaign(created_by=request.user, audience={'segment': 'all'})
        if not str(body.get('name') or '').strip():
            body = {**body, 'name': 'Untitled campaign'}
        try:
            _apply(campaign, body)
        except content.ContentError as error:
            return _content_error(error)
        except audience_mod.AudienceError as error:
            return Response({'detail': str(error), 'field': 'audience'}, status=status.HTTP_400_BAD_REQUEST)
        campaign.save()
        _audit(request, 'Created email campaign', campaign)
        return Response({'campaign': campaign_row(_load(campaign.pk))}, status=status.HTTP_201_CREATED)


def _delivery_info():
    from django.conf import settings
    mode = sender.delivery_mode()
    return {
        'mode': mode,
        'from': sender.from_address() if mode != 'off' else '',
        'webhookConfigured': bool(getattr(settings, 'RESEND_WEBHOOK_SECRET', '')),
        'emailConfigured': bool(settings.RESEND_API_KEY),
    }


class CampaignOptionsView(_View):

    def get(self, request):
        return Response({
            'segments': [{'key': k, 'label': v} for k, v in audience_mod.SEGMENTS.items()],
            'locations': audience_mod.location_options(),
            'categories': audience_mod.category_options(),
            # What customers' own bookings / favourites / tickets show interest in.
            'interests': audience_mod.interest_options(),
            'delivery': _delivery_info(),
            'limits': {**content.LIMITS, 'images': content.MAX_IMAGES,
                       'testRecipients': sender.MAX_TEST_RECIPIENTS},
        })


class CampaignAudienceView(_View):

    def post(self, request):
        try:
            chosen = audience_mod.clean_audience(self._body(request).get('audience'))
        except audience_mod.AudienceError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)
        result = audience_mod.estimate(chosen)
        result['audience'] = chosen
        result['label'] = audience_mod.describe(chosen)
        return Response(result)


class CampaignAudienceRecipientsView(_View):
    """The customers the chosen audience reaches — the SAME list a send would
    freeze (audience.recipients). Full addresses: this is the super admin's
    own audience check, behind IsAdmin like every campaign route. Nothing is
    sent from here."""
    PAGE = 25

    def post(self, request):
        body = self._body(request)
        try:
            chosen = audience_mod.clean_audience(body.get('audience'))
        except audience_mod.AudienceError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)
        try:
            page = max(1, int(body.get('page') or 1))
        except (TypeError, ValueError):
            page = 1
        search = str(body.get('search') or '')[:100]
        result = audience_mod.recipient_page(chosen, page=page, page_size=self.PAGE, search=search)
        result['audience'] = chosen
        result['label'] = audience_mod.describe(chosen)
        return Response(result)


class CampaignPreviewView(_View):
    """Render unsaved edits exactly as the email will look — the panel shows
    this HTML in a sandboxed frame. Nothing is stored."""

    def post(self, request):
        body = self._body(request)
        campaign = Campaign(name=str(body.get('name') or 'Preview')[:120] or 'Preview')
        try:
            content.apply_fields(campaign, {k: v for k, v in body.items() if k != 'name'})
        except content.ContentError as error:
            return _content_error(error)
        rendered = content.render_template(campaign)
        subject, html, text = content.personalize(
            rendered, name=request.user.name or '', unsubscribe_url='#unsubscribe-preview')
        return Response({'subject': subject, 'previewText': campaign.preview_text,
                         'html': html, 'text': text, 'from': sender.from_address()})


class CampaignImagesView(APIView):
    """GET the library; POST one image (multipart, X-Requested-With)."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    MAX_BYTES = 5 * 1024 * 1024
    MIN_WIDTH = 300

    def get(self, request):
        from adminpanel.models import Settings

        library, seen = [], set()

        def add(url, source, label):
            if url and url not in seen and str(url).startswith('https://'):
                seen.add(url)
                library.append({'url': url, 'source': source, 'label': label})

        for campaign in Campaign.objects.order_by('-updated_at').only('name', 'images')[:60]:
            for image in campaign.images or []:
                add(image.get('url'), 'campaign', campaign.name)
        for banner in Settings.load().banners or []:
            if isinstance(banner, dict):
                label = banner.get('title') or banner.get('code') or 'Homepage banner'
                add(banner.get('image'), 'banner', label)
                add(banner.get('imageMobile'), 'banner', f'{label} (mobile)')
        return Response({'images': library[:80]})

    def post(self, request):
        from adminpanel.image_info import image_size
        from venues.storage import ALLOWED_TYPES, StorageError, upload_photo
        from venues.views import _looks_like_image

        # See adminpanel.views.AdminUploadView: the custom header forces a
        # CORS preflight for this otherwise "simple" multipart POST.
        if not request.headers.get('X-Requested-With'):
            return detail('Uploads must send an X-Requested-With header.', status.HTTP_400_BAD_REQUEST)
        upload = request.FILES.get('image') or request.FILES.get('file')
        if upload is None:
            return detail('No file uploaded.', status.HTTP_400_BAD_REQUEST)
        extension = ALLOWED_TYPES.get(upload.content_type)
        if extension is None:
            return detail('Only JPEG, PNG or WebP images are allowed.', status.HTTP_400_BAD_REQUEST)
        if upload.size > self.MAX_BYTES:
            return detail(f'Image is too large (max {self.MAX_BYTES // (1024 * 1024)} MB). '
                          'Export it as a compressed JPEG.', status.HTTP_400_BAD_REQUEST)
        if not _looks_like_image(upload):
            return detail('That file is not a valid JPEG, PNG or WebP image.', status.HTTP_400_BAD_REQUEST)
        data = upload.read()
        size = image_size(data)
        if size is None:
            return detail('Could not read this image’s size. Re-save it as JPEG or PNG.',
                          status.HTTP_400_BAD_REQUEST)
        width, height = size
        if width < self.MIN_WIDTH:
            return detail(f'The image is only {width}px wide — use at least {self.MIN_WIDTH}px '
                          '(1200px looks sharp on every screen).', status.HTTP_400_BAD_REQUEST)
        warnings = []
        if width < 1200:
            warnings.append(f'{width}px wide — 1200px or more looks sharper on phones and retina screens.')
        if extension == 'webp':
            warnings.append('Some email apps (older Outlook) cannot show WebP — JPEG or PNG is safest.')
        path = f'campaigns/{uuid.uuid4().hex}.{extension}'
        try:
            url = upload_photo(path, data, upload.content_type)
        except StorageError as error:
            logger.warning('Campaign image upload failed: %s', error)
            return detail('Could not store the image right now. Please try again.',
                          status.HTTP_502_BAD_GATEWAY)
        from adminpanel.views import record_audit
        record_audit(request, 'Uploaded an email campaign image', upload.name or 'image',
                     f'{width}×{height}, {upload.size // 1024} KB')
        return Response({'url': url, 'width': width, 'height': height, 'warnings': warnings},
                        status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# One campaign
# ---------------------------------------------------------------------------

class CampaignDetailView(_View):

    def get(self, request, campaign_id):
        campaign = _load(campaign_id)
        if campaign is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        return Response({'campaign': campaign_row(campaign)})

    def patch(self, request, campaign_id):
        body = self._body(request)
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().filter(pk=campaign_id).first()
            if campaign is None:
                return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
            if campaign.status not in EDITABLE:
                return Response({'detail': 'A campaign that has been sent can no longer be edited. '
                                           'Duplicate it to send a new version.', 'code': 'NOT_EDITABLE'},
                                status=status.HTTP_409_CONFLICT)
            if str(body.get('version')) != str(campaign.version):
                return Response({'detail': 'Someone else saved this campaign since you opened it.',
                                 'code': 'STALE', 'campaign': campaign_row(_load(campaign.pk))},
                                status=status.HTTP_409_CONFLICT)
            try:
                _apply(campaign, body)
            except content.ContentError as error:
                return _content_error(error)
            except audience_mod.AudienceError as error:
                return Response({'detail': str(error), 'field': 'audience'},
                                status=status.HTTP_400_BAD_REQUEST)
            campaign.version += 1
            campaign.save()
        _audit(request, 'Saved email campaign draft', campaign, f'version {campaign.version}')
        return Response({'campaign': campaign_row(_load(campaign.pk))})

    def delete(self, request, campaign_id):
        campaign = Campaign.objects.filter(pk=campaign_id).first()
        if campaign is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        if campaign.status != Campaign.Status.DRAFT:
            return Response({'detail': 'Sent campaigns are kept as history and cannot be deleted.',
                             'code': 'NOT_EDITABLE'}, status=status.HTTP_409_CONFLICT)
        _audit(request, 'Deleted email campaign draft', campaign)
        campaign.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class CampaignDuplicateView(_View):

    def post(self, request, campaign_id):
        source = Campaign.objects.filter(pk=campaign_id).first()
        if source is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        copy = Campaign.objects.create(
            name=f'{source.name} (copy)'[:120], subject=source.subject,
            preview_text=source.preview_text, heading=source.heading, body=source.body,
            images=list(source.images or []), cta_label=source.cta_label,
            cta_url=source.cta_url, audience=dict(source.audience or {}),
            created_by=request.user,
        )
        _audit(request, 'Duplicated email campaign', copy, f'from {source.name}')
        return Response({'campaign': campaign_row(_load(copy.pk))}, status=status.HTTP_201_CREATED)


class CampaignTestView(_View):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'campaign_test'

    def post(self, request, campaign_id):
        campaign = Campaign.objects.filter(pk=campaign_id).first()
        if campaign is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        problems = content.send_problems(campaign)
        if problems:
            return Response({'detail': next(iter(problems.values())), 'code': 'INCOMPLETE',
                             'problems': problems}, status=status.HTTP_400_BAD_REQUEST)
        try:
            emails = sender.clean_test_recipients(
                self._body(request).get('to'), fallback=request.user.email or '')
        except ValueError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)
        try:
            results = sender.send_test(campaign, emails, admin=request.user,
                                       base=sender.public_base(request))
        except sender.DeliveryOff:
            return Response({'detail': 'Sending is switched off on this server '
                                       '(CAMPAIGN_DELIVERY=off). No test email was sent.',
                             'code': 'DELIVERY_OFF'}, status=status.HTTP_409_CONFLICT)
        except resend_client.SendError as error:
            logger.warning('Campaign test email failed: %s', error)
            return Response({'detail': f'Resend refused the test email: {error}', 'code': 'SEND_FAILED'},
                            status=status.HTTP_502_BAD_GATEWAY)
        dry = sender.delivery_mode() == 'dry-run'
        _audit(request, 'Sent email campaign test', campaign,
               f'{len(emails)} address(es){" (dry run)" if dry else ""}')
        return Response({'sent': len(results), 'dryRun': dry, 'to': emails})


class CampaignSendView(_View):

    def post(self, request, campaign_id):
        body = self._body(request)
        campaign = Campaign.objects.filter(pk=campaign_id).first()
        if campaign is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        if body.get('confirm') is not True:
            return Response({'detail': 'Confirm the send first.', 'code': 'CONFIRM_REQUIRED'},
                            status=status.HTTP_400_BAD_REQUEST)
        if (campaign.audience or {}).get('segment', 'all') == 'all' and body.get('confirmAll') is not True:
            return Response({'detail': 'Sending to ALL eligible customers needs the extra confirmation.',
                             'code': 'CONFIRM_ALL_REQUIRED'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            expected = int(body.get('expectedCount'))
        except (TypeError, ValueError):
            expected = None
        try:
            sender.queue(campaign.pk, admin=request.user, expected_count=expected,
                         version=body.get('version'), base=sender.public_base(request))
        except sender.QueueRefused as refusal:
            code = status.HTTP_400_BAD_REQUEST if refusal.code in ('INCOMPLETE', 'NO_RECIPIENTS') \
                else status.HTTP_409_CONFLICT
            return Response({'detail': str(refusal), 'code': refusal.code, **refusal.extra}, status=code)
        fresh = _load(campaign.pk)
        _audit(request, 'Sent email campaign', fresh,
               f'{fresh.stat_recipients} recipients · {audience_mod.describe(fresh.audience)}'
               f'{" · dry run" if fresh.dry_run else ""}')
        return Response({'campaign': campaign_row(fresh)}, status=status.HTTP_202_ACCEPTED)


class CampaignCancelView(_View):

    def post(self, request, campaign_id):
        campaign = sender.cancel(campaign_id)
        if campaign is None:
            return Response({'detail': 'Only a queued or sending campaign can be cancelled.',
                             'code': 'NOT_CANCELLABLE'}, status=status.HTTP_409_CONFLICT)
        _audit(request, 'Cancelled email campaign', campaign)
        return Response({'campaign': campaign_row(_load(campaign.pk))})


class CampaignRecipientsView(_View):
    PAGE = 50

    def get(self, request, campaign_id):
        campaign = Campaign.objects.filter(pk=campaign_id).first()
        if campaign is None:
            return detail('Campaign not found.', status.HTTP_404_NOT_FOUND)
        rows = campaign.recipients.all()
        wanted = str(request.query_params.get('status') or '')
        if wanted == 'problems':
            rows = rows.filter(status__in=(R.FAILED, R.BOUNCED, R.COMPLAINED))
        elif wanted in R.values:
            rows = rows.filter(status=wanted)
        try:
            page = max(1, int(request.query_params.get('page') or 1))
        except ValueError:
            page = 1
        total = rows.count()
        chunk = rows.order_by('pk')[(page - 1) * self.PAGE: page * self.PAGE]
        return Response({
            'total': total, 'page': page, 'pageSize': self.PAGE,
            'recipients': [{
                'id': r.pk,
                'name': r.name,
                'email': audience_mod.mask_email(r.email),
                'status': r.status,
                'statusLabel': r.get_status_display(),
                'error': r.error,
                'attempts': r.attempts,
                'sentAt': _iso(r.sent_at), 'deliveredAt': _iso(r.delivered_at),
                'openedAt': _iso(r.opened_at), 'clickedAt': _iso(r.clicked_at),
                'unsubscribedAt': _iso(r.unsubscribed_at),
            } for r in chunk],
        })
