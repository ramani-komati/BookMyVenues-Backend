"""
The Explore homepage, controlled by the super admin (Homepage page in the
admin panel) and served to customers in ONE cached read.

    GET  /api/homepage                                    public, cached
    GET  /api/admin/homepage                              everything the page edits
    PUT  /api/admin/homepage/sections                     switches, titles, order
    POST /api/admin/homepage/banners                      create a banner / promotion
    PATCH/DELETE /api/admin/homepage/banners/<id>         edit / delete (when safe)
    POST /api/admin/homepage/banners/reorder              hero or promotions order
    POST /api/admin/homepage/banner-image                 upload banner artwork
    POST /api/admin/homepage/featured-events              feature an existing event
    PATCH/DELETE /api/admin/homepage/featured-events/<id> schedule / stop featuring
    POST /api/admin/homepage/featured-events/reorder
    POST /api/admin/homepage/featured-venues/reorder

NO SECOND PROMOTION SYSTEM. Hero banners and platform promotions are the SAME
records the platform already had — Settings.banners, whose codes checkout
validates (bookings.views._apply_platform_offer, unchanged). This module only
adds optional keys to each record:

    published          homepage switch (default True — every existing banner
                       was live). Hides the record from the homepage; its code
                       keeps working at checkout until its Valid-to date, so
                       redemption rules are exactly what they were.
    showInHero         appears in the hero carousel (default True)
    featuredPromotion  appears in Featured Promotions (default True)
    heroOrder / promoOrder   position in each (default: list order)
    image / imageMobile / imageAlt   uploaded artwork (desktop + optional phone)
    imagePosition / imageMobilePosition   which part of the art stays in view
                       when a frame must crop it (banners with words drawn
                       over the art): one of IMAGE_POSITIONS, default centre
    showText / showCode      draw the title/details and the code over the image
    ctaLabel / ctaUrl        button text and destination
    category           a customer nav category the promotion is aimed at —
                       display and destination only; checkout still accepts
                       the code on any venue, as it always has
    terms              optional free-text terms
    createdAt / updatedAt / updatedBy   admin bookkeeping (never public)

Featured events reuse Event.featured (+ the new optional featured_order /
featured_from / featured_until); featured venues reuse Listing.featured, with
their order kept in Settings.homepage. Nothing here creates, copies or deletes
an event or a venue.

CONCURRENCY. Every write locks the Settings row (select_for_update) and edits
only what it was asked to: two admins editing different banners never undo
each other. Whole-list writes (sections, reorders) carry the revision the
admin loaded and are refused with 409 when someone saved in between; a banner
edit carries the banner's updatedAt for the same reason.

CACHING. The public payload is cached per process, keyed on
Settings.homepage_revision + public_version + today's date. Any admin write,
rating, event promotion or venue feature bumps a version, so every process
stops serving the old payload at once; the 60-second expiry covers the rest
(an event selling out, a vendor edit).
"""
import datetime
import logging
import re
import uuid

from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from bookings.slots import today_ist

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from .image_info import image_size
from .models import Settings

logger = logging.getLogger(__name__)

CACHE_SECONDS = 60

# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

# Always first, in this order — the brief's homepage order. Each can be
# switched off and retitled, but not moved.
FIXED_SECTIONS = ('hero', 'topEvents', 'featuredPromotions')
# The existing venue-discovery sections, in today's order. The admin may
# reorder these and switch them off — except `venues`, the main catalogue,
# which always shows (a homepage with no way to browse venues is broken).
MOVABLE_SECTIONS = ('spotlight', 'topPromotions', 'venues', 'newVenues')
REQUIRED_SECTIONS = ('venues',)
# Removed from the site. Saved layouts and older admin clients may still
# carry them; they are dropped quietly instead of refused.
RETIRED_SECTIONS = ('howItWorks',)
# No longer drawn as rows of their own: both repeated the main venue list.
# They stay in the config (saved layouts and older admin clients still send
# them) and are reported as merged into 'venues'. 'spotlight' on/off now
# means "the featured venues lead the venue list"; 'newVenues' has no
# setting left — new venues carry a "New" tag on their card instead.
MERGED_SECTIONS = {'spotlight': 'venues', 'newVenues': 'venues'}

# `{area}` is replaced on the customer site with the chosen area ("Warangal").
# Blank venue title = the customer site picks "Venues near you" / "Explore venues in {area}".
SECTION_DEFAULTS = {
    'hero': {
        'label': 'Hero banners',
        'help': 'The rotating image banner at the top of Explore.',
        'title': '', 'subtitle': '',
    },
    'topEvents': {
        'label': 'Top events',
        'help': 'Featured events, straight after the hero.',
        'title': 'Top events', 'subtitle': 'Handpicked events you can book now',
    },
    'featuredPromotions': {
        'label': 'Featured promotions',
        'help': 'Platform offers and codes, after Top events.',
        'title': 'Featured promotions', 'subtitle': 'Platform offers you can use on any venue',
    },
    'spotlight': {
        'label': 'Featured venues first',
        'help': 'Merged into the venue list: the venues you feature (Featured '
                'venues tab) lead it, tagged “Featured”. No separate row.',
        'title': '', 'subtitle': '',
    },
    'topPromotions': {
        'label': 'Top promotions (venue offers)',
        'help': "Vendors' own offers, one per venue.",
        'title': 'Top promotions', 'subtitle': 'Offers from venues in {area}',
    },
    'venues': {
        'label': 'Venues (the one venue list)',
        'help': 'Every venue once, with distance filters. Always shown. Leave '
                'the title blank for “Venues near you” (customer shared a '
                'location) or “Explore venues in {area}”.',
        'title': '', 'subtitle': '',
    },
    'newVenues': {
        'label': 'Newly added',
        'help': 'Merged into the venue list: recently listed venues carry a '
                '“New” tag on their card. No separate row.',
        'title': '', 'subtitle': '',
    },
}

TOP_EVENTS_DEFAULT_LIMIT = 6
TOP_EVENTS_MAX_LIMIT = 12
MAX_FEATURED_VENUES = 12
TITLE_MAX = 80
SUBTITLE_MAX = 160


def _section_record(key, stored):
    defaults = SECTION_DEFAULTS[key]
    stored = stored if isinstance(stored, dict) else {}
    title = stored.get('title')
    subtitle = stored.get('subtitle')
    record = {
        'key': key,
        'label': defaults['label'],
        'help': defaults['help'],
        'fixed': key in FIXED_SECTIONS,
        'required': key in REQUIRED_SECTIONS,
        'mergedInto': MERGED_SECTIONS.get(key),
        'enabled': True if key in REQUIRED_SECTIONS else stored.get('enabled') is not False,
        # A section never saved keeps the built-in wording.
        'title': title.strip() if isinstance(title, str) else defaults['title'],
        'subtitle': subtitle.strip() if isinstance(subtitle, str) else defaults['subtitle'],
        'defaultTitle': defaults['title'],
        'defaultSubtitle': defaults['subtitle'],
    }
    if key == 'topEvents':
        try:
            limit = int(stored.get('limit', TOP_EVENTS_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = TOP_EVENTS_DEFAULT_LIMIT
        record['limit'] = max(1, min(TOP_EVENTS_MAX_LIMIT, limit))
    return record


def homepage_config(settings_obj):
    """The homepage layout with every gap filled by its default:
    {'sections': [...render order...], 'featuredVenueOrder': [ids]}."""
    raw = settings_obj.homepage if isinstance(settings_obj.homepage, dict) else {}
    stored = {}
    stored_order = []
    for entry in raw.get('sections') or []:
        if isinstance(entry, dict) and entry.get('key') in SECTION_DEFAULTS:
            if entry['key'] not in stored:
                stored[entry['key']] = entry
                stored_order.append(entry['key'])
    movable = [k for k in stored_order if k in MOVABLE_SECTIONS]
    movable += [k for k in MOVABLE_SECTIONS if k not in movable]
    order = list(FIXED_SECTIONS) + movable
    venue_order = [
        str(pk) for pk in (raw.get('featuredVenueOrder') or []) if isinstance(pk, str)
    ]
    return {
        'sections': [_section_record(key, stored.get(key)) for key in order],
        'featuredVenueOrder': venue_order,
    }


def _next_stamp(previous):
    """A timestamp strictly after `previous` — the edit guard compares these, and
    on a coarse clock (Windows ticks every ~15 ms) two saves in one tick would
    otherwise get the same stamp and a stale edit would slip through."""
    now = timezone.now()
    try:
        last = datetime.datetime.fromisoformat(str(previous)) if previous else None
    except ValueError:
        last = None
    if last is not None and now <= last:
        now = last + datetime.timedelta(microseconds=1)
    return now.isoformat()


def _section(config, key):
    return next(s for s in config['sections'] if s['key'] == key)


# ---------------------------------------------------------------------------
# Banners / promotions (records in Settings.banners)
# ---------------------------------------------------------------------------

BANNER_TYPES = ('none', 'complimentary', 'percent', 'flat')
DISCOUNT_TYPES = ('percent', 'flat')
# The customer site's nav categories (frontend features/home/data.js).
CATEGORIES = (
    ('celebrate', 'Celebrate'),
    ('sports', 'Sports'),
    ('gaming', 'Gaming'),
    ('resorts', 'Resorts'),
    ('events', 'Events'),
    ('fitness', 'Fitness'),
)
CATEGORY_KEYS = {key for key, _ in CATEGORIES}
CODE_RE = re.compile(r'^[A-Z0-9]{1,30}$')
DEFAULT_CTA = 'Explore venues'
# Admin bookkeeping that never reaches a customer.
_ADMIN_ONLY_KEYS = (
    'published', 'showInHero', 'featuredPromotion', 'heroOrder', 'promoOrder',
    'createdAt', 'updatedAt', 'updatedBy',
    'imageWidth', 'imageHeight', 'imageMobileWidth', 'imageMobileHeight',
)
_TEXT_LIMITS = {
    'title': 120, 'text': 240, 'terms': 600, 'imageAlt': 200, 'ctaLabel': 40,
}
_SAFE_IMAGE_PREFIXES = ('https://', 'http://', '/')
# The focal points an admin can pick for each artwork (CSS object-position
# keywords — the customer site and the panel preview use them verbatim).
IMAGE_POSITIONS = ('center', 'top', 'bottom', 'left', 'right')
# Payment statuses where the money was captured — a booking in one of these
# that used a code is part of that promotion's redemption history.
REDEMPTION_STATUSES = ('confirmed', 'refund_pending', 'refunded')


def _bool(value, default):
    return value if isinstance(value, bool) else default


def _int(value, default=0):
    try:
        return int(float(str(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def _parse_date(text):
    try:
        return datetime.date.fromisoformat(str(text or '').strip())
    except ValueError:
        return None


def banner_id(raw, index):
    """A banner's id as a string. Banners saved by the old Settings form
    carry a numeric Date.now() id; a few very old ones have none and are
    addressed by position until their first edit gives them one."""
    value = raw.get('id') if isinstance(raw, dict) else None
    return str(value) if value not in (None, '') else f'banner-{index}'


def normalize_banner(raw, index):
    """One stored banner with every optional key defaulted — the shape both
    the admin page and the public payload start from. Never mutates `raw`."""
    b = dict(raw)
    b['id'] = banner_id(raw, index)
    b['title'] = str(raw.get('title') or '').strip()
    b['text'] = str(raw.get('text') or '').strip()
    b['type'] = str(raw.get('type') or 'none').strip().lower()
    if b['type'] not in BANNER_TYPES:
        b['type'] = 'none'
    b['value'] = _int(raw.get('value'))
    b['code'] = str(raw.get('code') or '').strip().upper()
    b['minAmount'] = _int(raw.get('minAmount'))
    b['maxDiscount'] = _int(raw.get('maxDiscount'))
    b['perUserLimit'] = _int(raw.get('perUserLimit'))
    b['from'] = str(raw.get('from') or '')
    b['to'] = str(raw.get('to') or '')
    for key in ('image', 'imageMobile', 'imageAlt', 'ctaUrl', 'category', 'terms'):
        b[key] = str(raw.get(key) or '').strip()
    # Every banner that existed before these switches was live in both places.
    b['published'] = _bool(raw.get('published'), True)
    b['showInHero'] = _bool(raw.get('showInHero'), True)
    b['featuredPromotion'] = _bool(raw.get('featuredPromotion'), True)
    b['showText'] = _bool(raw.get('showText'), True)
    b['showCode'] = _bool(raw.get('showCode'), True)
    # The hero button always read "Explore venues" — keep it unless changed.
    b['ctaLabel'] = (
        str(raw['ctaLabel']).strip() if isinstance(raw.get('ctaLabel'), str) else DEFAULT_CTA
    )
    b['heroOrder'] = _int(raw.get('heroOrder'), index)
    b['promoOrder'] = _int(raw.get('promoOrder'), index)
    for key in ('imagePosition', 'imageMobilePosition'):
        value = str(raw.get(key) or '').strip().lower()
        b[key] = value if value in IMAGE_POSITIONS else 'center'
    return b


def banner_state(b, today):
    """'draft' | 'scheduled' | 'expired' | 'live' — what the homepage does
    with it today."""
    if not b['published']:
        return 'draft'
    start, end = _parse_date(b['from']), _parse_date(b['to'])
    if start is not None and start > today:
        return 'scheduled'
    if end is not None and end < today:
        return 'expired'
    return 'live'


def is_usable_promotion(b):
    """A promotion a customer can rely on: an announcement (nothing to
    redeem), or a discount that actually has a code and a value."""
    if not b['title']:
        return False
    if b['type'] in DISCOUNT_TYPES:
        return bool(b['code']) and b['value'] > 0
    return True


def public_banner(b):
    """The customer-facing record: no admin bookkeeping, and announcements
    never carry a code or spend terms even if stray values were stored."""
    out = {k: v for k, v in b.items() if k not in _ADMIN_ONLY_KEYS}
    if b['type'] not in DISCOUNT_TYPES:
        out.update({'code': '', 'value': 0, 'minAmount': 0, 'maxDiscount': 0, 'perUserLimit': 0})
    if not out.get('showCode', True):
        out['showCode'] = False
    return out


def all_banners(settings_obj):
    return [
        normalize_banner(raw, index)
        for index, raw in enumerate(settings_obj.banners or [])
        if isinstance(raw, dict)
    ]


def active_banners(settings_obj, today=None):
    """Published banners inside their dates, in stored order — what
    GET /api/banners has always served (it now also respects `published`)."""
    today = today or today_ist()
    return [
        b for b in all_banners(settings_obj)
        if b['title'] and banner_state(b, today) == 'live'
    ]


def hero_banners(settings_obj, today=None):
    rows = [b for b in active_banners(settings_obj, today) if b['showInHero']]
    return sorted(rows, key=lambda b: b['heroOrder'])


def featured_promotions(settings_obj, today=None):
    rows = [
        b for b in active_banners(settings_obj, today)
        if b['featuredPromotion'] and is_usable_promotion(b)
    ]
    return sorted(rows, key=lambda b: b['promoOrder'])


def redemption_counts():
    """{CODE: bookings that redeemed it with captured money} — one query."""
    from bookings.models import Booking

    rows = (
        Booking.objects.filter(offer__source='platform', status__in=REDEMPTION_STATUSES)
        .values('offer__code').annotate(n=Count('id'))
    )
    return {
        str(row['offer__code'] or '').strip().upper(): row['n'] for row in rows
        if row['offer__code']
    }


def _safe_url(value, *, allow_relative=True):
    text = str(value or '').strip()
    if not text:
        return True
    lowered = text.lower()
    if lowered.startswith('https://') or lowered.startswith('http://'):
        return True
    return allow_relative and text.startswith('/') and not text.startswith('//')


def banner_issues(b):
    """Plain-language problems the admin should fix (shown on the list)."""
    issues = []
    if b['showInHero'] and not b['image']:
        issues.append('Text only — add a banner image to show it as an image banner.')
    if b['type'] in DISCOUNT_TYPES and not b['code']:
        issues.append('Discount without a code — customers cannot redeem it.')
    width, height = _int(b.get('imageWidth')), _int(b.get('imageHeight'))
    if b['image'] and width and height:
        issues.extend(ratio_warnings('desktop', width, height))
    m_width, m_height = _int(b.get('imageMobileWidth')), _int(b.get('imageMobileHeight'))
    if b['imageMobile'] and m_width and m_height:
        issues.extend(ratio_warnings('mobile', m_width, m_height))
    return issues


# Fields the admin sends; everything else in the body is ignored.
_BANNER_FIELDS = (
    'title', 'text', 'type', 'value', 'code', 'minAmount', 'maxDiscount',
    'perUserLimit', 'from', 'to', 'image', 'imageMobile', 'imageAlt',
    'imageWidth', 'imageHeight', 'imageMobileWidth', 'imageMobileHeight',
    'showText', 'showCode', 'ctaLabel', 'ctaUrl', 'category', 'terms',
    'published', 'showInHero', 'featuredPromotion',
    'imagePosition', 'imageMobilePosition',
)
_BOOL_FIELDS = ('showText', 'showCode', 'published', 'showInHero', 'featuredPromotion')


class BannerError(Exception):
    def __init__(self, message, code='INVALID_BANNER', http_status=status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def clean_banner(body, current, others, redemptions):
    """Validate an admin create/edit and return the stored dict.

    `current` is the normalized existing banner (None when creating);
    `others` the other banners (for code uniqueness); `redemptions` the
    {CODE: n} map. Raises BannerError with a message the admin can act on.
    """
    merged = dict(current or {})
    for key in _BANNER_FIELDS:
        if key in body:
            merged[key] = body[key]
    for key in _BOOL_FIELDS:
        if key in body and not isinstance(body[key], bool):
            raise BannerError(f'{key} must be true or false.')
    # Checked on the RAW value: normalize_banner reads anything unknown as
    # 'center', which would hide a bad request instead of refusing it.
    for key in ('imagePosition', 'imageMobilePosition'):
        if key in body and str(body[key] or 'center').strip().lower() not in IMAGE_POSITIONS:
            raise BannerError(f'{key} must be one of: {", ".join(IMAGE_POSITIONS)}.')

    b = normalize_banner(merged, 0)
    if not b['title']:
        raise BannerError('Give the banner a title — it is also the image’s description for screen readers.')
    for key, limit in _TEXT_LIMITS.items():
        if len(b[key]) > limit:
            raise BannerError(f'{key} can be at most {limit} characters.')
    if str(merged.get('type') or 'none').strip().lower() not in BANNER_TYPES:
        raise BannerError('type must be none, complimentary, percent or flat.')

    if b['type'] in DISCOUNT_TYPES:
        if b['value'] <= 0:
            raise BannerError('Enter the discount for this offer.')
        if b['type'] == 'percent' and b['value'] > 100:
            raise BannerError('A percent discount can’t exceed 100.')
        if not b['code']:
            raise BannerError('Enter a promo code — customers apply it at checkout.')
        if not CODE_RE.match(b['code']):
            raise BannerError('Promo codes use letters A–Z and digits only (up to 30).')
        if any(o['code'] == b['code'] for o in others):
            raise BannerError(
                f'Code {b["code"]} is already used by another promotion.',
                code='DUPLICATE_CODE', http_status=status.HTTP_409_CONFLICT,
            )
    else:
        # Announcements carry nothing redeemable.
        b.update({'value': 0, 'code': '', 'minAmount': 0, 'maxDiscount': 0, 'perUserLimit': 0})
    for key in ('minAmount', 'maxDiscount', 'perUserLimit'):
        if b[key] < 0:
            raise BannerError(f'{key} can’t be negative.')
    if b['type'] != 'percent':
        b['maxDiscount'] = 0

    # Once customers have paid with a code, the code and what kind of
    # discount it is are the promotion's identity: changing them would make
    # its redemption history (and per-customer limits) describe a different
    # offer. Content, artwork, dates and amounts stay editable.
    if current is not None:
        used = redemptions.get(current['code'], 0) if current['code'] else 0
        if used and (b['code'] != current['code'] or b['type'] != current['type']):
            raise BannerError(
                f'Code {current["code"]} has been used on {used} paid '
                f'booking{"s" if used != 1 else ""}, so its code and discount type '
                'can’t change. Create a new promotion instead.',
                code='PROMOTION_REDEEMED', http_status=status.HTTP_409_CONFLICT,
            )

    for key in ('from', 'to'):
        if b[key] and _parse_date(b[key]) is None:
            raise BannerError(f'"{key}" must be a date (YYYY-MM-DD).')
    if b['from'] and b['to'] and b['from'] > b['to']:
        raise BannerError('"Valid from" must be on or before "Valid to".')

    for key in ('image', 'imageMobile'):
        if b[key] and not b[key].lower().startswith(_SAFE_IMAGE_PREFIXES):
            raise BannerError(f'{key} must be an https:// image URL.')
    if b['imageMobile'] and not b['image']:
        raise BannerError('Add the desktop image first — the phone image is an optional extra.')
    if not _safe_url(b['ctaUrl']):
        raise BannerError('The button link must start with / (a page on this site) or https://.')
    if len(b['ctaUrl']) > 500:
        raise BannerError('The button link is too long.')
    if b['category'] and b['category'] not in CATEGORY_KEYS:
        raise BannerError('Pick one of the listed categories.')
    for key in ('imageWidth', 'imageHeight', 'imageMobileWidth', 'imageMobileHeight'):
        b[key] = max(0, min(20000, _int(b.get(key))))

    # The product rule: a hero banner is an IMAGE banner. A published hero
    # banner without artwork is refused; an unpublished draft may wait for
    # its image, and a promotion shown only in Featured Promotions needs none.
    if b['published'] and b['showInHero'] and not b['image']:
        raise BannerError(
            'Hero banners need an image. Upload one, or untick “Show in hero” '
            'to use this only as a Featured Promotion.',
            code='IMAGE_REQUIRED',
        )
    return b


# ---------------------------------------------------------------------------
# Banner image specification — derived from the REAL hero CSS
# ---------------------------------------------------------------------------
#
# Customer site, features/home/home.css + HeroCarousel.jsx:
#   section padding 40px each side (16px on phones, ≤640px), no max-width,
#   .hm-hero  min-height: clamp(280px, 23vw, 420px)   (168px on phones)
#   .hm-hero-img  object-fit: cover, centred
# With no text over the image the content is shorter than the min-height, so
# the banner frame is exactly (viewport − 80) × clamp(280, 23vw, 420) on
# screens wider than 640px, and (viewport − 32) × 168 on phones:
#   1920px → 1840×420 (4.38:1)   1440px → 1360×331 (4.11:1)
#   1280px → 1200×294 (4.08:1)   1024px →  944×280 (3.37:1)
#    768px →  688×280 (2.46:1)    390px →  358×168 (2.13:1)   360px → 328×168 (1.95:1)
# Laptops and desktops (1217px and up, where 23vw takes over) sit at 4.0–4.4:1,
# so 4:1 is the design ratio; 2400×600 covers the largest frame at 1x and a
# 1440px retina laptop at ~1.8x. Phones sit at ~2:1, hence the optional 2:1
# phone image. A test (adminpanel/test_homepage.py) pins these numbers to the
# formula, so a CSS change that moves them has to update both.

def hero_frame(viewport_width):
    """(width, height) of the hero banner, image-only, at a viewport width."""
    if viewport_width <= 640:
        return viewport_width - 32, 168
    height = min(420, max(280, viewport_width * 0.23))
    return viewport_width - 80, round(height)


PREVIEW_VIEWPORTS = (
    ('Large desktop', 1920), ('Laptop', 1440), ('Small laptop', 1024),
    ('Tablet', 768), ('Phone', 390),
)

BANNER_MAX_BYTES = 5 * 1024 * 1024
BANNER_IMAGE_SPEC = {
    'formats': ['JPEG', 'PNG', 'WebP'],
    'mimeTypes': ['image/jpeg', 'image/png', 'image/webp'],
    'maxBytes': BANNER_MAX_BYTES,
    'slots': {
        'desktop': {
            'label': 'Banner image (desktop, laptop & tablet)',
            'usedAt': 'Screens wider than 640px',
            'recommended': {'width': 2400, 'height': 600},
            'sharpFrom': {'width': 1920, 'height': 480},
            'minimum': {'width': 1200, 'height': 300},
            'ratio': {'label': '4:1', 'value': 4.0, 'warnMin': 3.6, 'warnMax': 4.4,
                      'min': 2.5, 'max': 6.0},
            'safeZone': 'Keep words and logos inside the middle 80% of the width '
                        'and 90% of the height. Small laptops trim about 8% off '
                        'each side; tablets (641–1023px) show only the middle '
                        '60–85% of the width.',
        },
        'mobile': {
            'label': 'Phone image (optional)',
            'usedAt': 'Phones (640px and narrower) and the Featured promotions card (2:1)',
            'recommended': {'width': 1200, 'height': 600},
            'sharpFrom': {'width': 1080, 'height': 540},
            'minimum': {'width': 600, 'height': 300},
            'ratio': {'label': '2:1', 'value': 2.0, 'warnMin': 1.8, 'warnMax': 2.3,
                      'min': 1.4, 'max': 3.0},
            'safeZone': 'Without a phone image, phones crop the desktop image to '
                        'its middle half, and the Featured promotions card shows '
                        'the desktop image whole on a blurred backdrop.',
        },
    },
    'frames': [
        {'label': label, 'viewport': vw, 'width': hero_frame(vw)[0], 'height': hero_frame(vw)[1]}
        for label, vw in PREVIEW_VIEWPORTS
    ],
    'notes': [
        'The image fills the banner and is trimmed (never stretched) to fit '
        'each screen — the previews show exactly what each screen keeps.',
        'With text shown over the image, the banner grows taller on narrow '
        'screens and trims more off the sides.',
    ],
}


def ratio_warnings(slot, width, height):
    """Soft warnings for an image that will upload but may not look right."""
    spec = BANNER_IMAGE_SPEC['slots'][slot]
    ratio = width / height if height else 0
    out = []
    name = 'phone image' if slot == 'mobile' else 'banner image'
    if not spec['ratio']['warnMin'] <= ratio <= spec['ratio']['warnMax']:
        out.append(
            f'The {name} is {width}×{height} ({ratio:.2f}:1); banners are '
            f'designed for {spec["ratio"]["label"]}, so more of it will be trimmed.'
        )
    sharp = spec['sharpFrom']
    if width < sharp['width'] or height < sharp['height']:
        out.append(
            f'The {name} is {width}×{height}; at least {sharp["width"]}×'
            f'{sharp["height"]} looks sharp on large and high-resolution screens.'
        )
    return out


def image_problem(slot, width, height):
    """A hard refusal for an image that would be blurry or badly cropped."""
    spec = BANNER_IMAGE_SPEC['slots'][slot]
    minimum = spec['minimum']
    if width > 8000 or height > 8000:
        return 'Images larger than 8000px on a side are not accepted — resize it first.'
    if width < minimum['width'] or height < minimum['height']:
        return (
            f'This image is {width}×{height}px — too small to stay sharp. Use at '
            f'least {minimum["width"]}×{minimum["height"]}px '
            f'(recommended {spec["recommended"]["width"]}×{spec["recommended"]["height"]}px).'
        )
    ratio = width / height
    if not spec['ratio']['min'] <= ratio <= spec['ratio']['max']:
        return (
            f'This image is {ratio:.2f}:1 — too far from the banner’s '
            f'{spec["ratio"]["label"]} shape; most of it would be cut off. Crop it '
            f'to about {spec["recommended"]["width"]}×{spec["recommended"]["height"]}px.'
        )
    return None


# ---------------------------------------------------------------------------
# Public payload
# ---------------------------------------------------------------------------

def _top_events(limit):
    from bookings.ratings import event_ratings_map
    from events.models import Event
    from events.public_views import (
        FEATURED_ORDERING,
        event_summary,
        featured_events_q,
        live_events,
    )

    rows = list(
        live_events().filter(featured_events_q()).distinct()
        .order_by(*FEATURED_ORDERING)[:Event.MAX_FEATURED]
    )
    ratings = event_ratings_map([row.pk for row in rows])
    summaries = [event_summary(row, ratings) for row in rows]
    # Bookable right now: inside its selling window with a night not sold
    # out — the same `bookable` the event card's Book button relies on.
    return [s for s in summaries if s['bookable']][:limit]


def _featured_venues(order):
    from bookings.ratings import venue_ratings_map
    from venues.models import Listing
    from venues.public_views import _summary

    rows = [
        listing for listing in
        Listing.objects.filter(featured=True, status=Listing.Status.LIVE).order_by('-updated_at')
        # A "— Pitch 2" unit is part of its base venue, never its own card.
        if not str(((listing.record or {}).get('detail') or {}).get('unitOf') or '').strip()
    ]
    position = {pk: index for index, pk in enumerate(order)}
    rows.sort(key=lambda l: position.get(str(l.id), len(position)))
    rows = rows[:MAX_FEATURED_VENUES]
    ratings = venue_ratings_map(rows)
    return [_summary(listing, ratings) for listing in rows]


def build_public_homepage(settings_obj):
    config = homepage_config(settings_obj)
    enabled = {s['key'] for s in config['sections'] if s['enabled']}
    today = today_ist()
    return {
        'sections': [
            {k: s[k] for k in ('key', 'enabled', 'title', 'subtitle', 'limit') if k in s}
            for s in config['sections']
        ],
        'hero': (
            [public_banner(b) for b in hero_banners(settings_obj, today)]
            if 'hero' in enabled else []
        ),
        'topEvents': (
            _top_events(_section(config, 'topEvents')['limit'])
            if 'topEvents' in enabled else []
        ),
        'promotions': (
            [public_banner(b) for b in featured_promotions(settings_obj, today)]
            if 'featuredPromotions' in enabled else []
        ),
        # Admin-curated venues lead the venue list (tagged "Featured"). Always sent (even with
        # the switch off) — it is a short list and costs one query.
        'featuredVenues': _featured_venues(config['featuredVenueOrder']),
        'generatedAt': timezone.now().isoformat(),
    }


def public_cache_key(settings_obj=None):
    stamp = (
        (settings_obj.homepage_revision, settings_obj.public_version)
        if settings_obj is not None else
        Settings.objects.filter(pk=1).values_list('homepage_revision', 'public_version').first()
    ) or (0, 0)
    return f'homepage:v1:{stamp[0]}:{stamp[1]}:{today_ist().isoformat()}'


class PublicHomepageView(APIView):
    """GET /api/homepage — everything the Explore page's admin-controlled
    sections show, published content only, in one cached response."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'public'

    def get(self, request):
        key = public_cache_key()
        payload = cache.get(key)
        if payload is None:
            settings_obj = Settings.load()
            payload = build_public_homepage(settings_obj)
            cache.set(key, payload, CACHE_SECONDS)
        return Response(payload)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def _event_row(event, today, ratings):
    from events.public_views import date_rows

    dates = date_rows(event)
    average, count = ratings.get(str(event.id), (None, 0))
    start, end = event.featured_from, event.featured_until
    if event.status != event.Status.LIVE:
        state = 'not_live'
    elif start and start > today:
        state = 'scheduled'
    elif end and end < today:
        state = 'ended'
    elif not any(d['available'] for d in dates):
        state = 'not_bookable'
    else:
        state = 'showing'
    return {
        'id': str(event.id),
        'name': event.name,
        'category': event.category,
        'status': event.status,
        'image': event.poster_url,
        'location': event.location,
        'nextDate': dates[0]['date'] if dates else None,
        'featured': event.featured,
        'featuredAt': event.featured_at.isoformat() if event.featured_at else None,
        'featuredOrder': event.featured_order,
        'featuredFrom': start.isoformat() if start else '',
        'featuredUntil': end.isoformat() if end else '',
        'state': state,
        'rating': average,
        'ratingCount': count,
    }


def admin_payload(settings_obj):
    from bookings.ratings import event_ratings_map, venue_ratings_map
    from events.models import Event
    from events.public_views import FEATURED_ORDERING
    from venues.models import Listing

    today = today_ist()
    config = homepage_config(settings_obj)
    counts = redemption_counts()
    banners = []
    for b in all_banners(settings_obj):
        row = dict(b)
        row['state'] = banner_state(b, today)
        row['redemptions'] = counts.get(b['code'], 0) if b['code'] else 0
        row['usablePromotion'] = is_usable_promotion(b)
        row['issues'] = banner_issues(b)
        banners.append(row)

    events = list(
        Event.objects.filter(featured=True).prefetch_related('dates', 'ticket_types')
        .order_by(*FEATURED_ORDERING)
    )
    event_ratings = event_ratings_map([e.pk for e in events])
    venues = list(Listing.objects.filter(featured=True).exclude(status=Listing.Status.DELETED))
    position = {pk: i for i, pk in enumerate(config['featuredVenueOrder'])}
    venues.sort(key=lambda l: (position.get(str(l.id), len(position)), l.name.lower()))
    venue_ratings = venue_ratings_map(venues)
    return {
        'revision': settings_obj.homepage_revision,
        'sections': config['sections'],
        'banners': banners,
        'featuredEvents': [_event_row(e, today, event_ratings) for e in events],
        'featuredVenues': [
            {
                'id': str(l.id),
                'name': l.name,
                'category': l.category,
                'locality': l.locality,
                'status': l.status,
                'image': (l.record or {}).get('image') or '',
                'rating': venue_ratings.get(str(l.id), (None, 0))[0],
                'ratingCount': venue_ratings.get(str(l.id), (None, 0))[1],
            }
            for l in venues
        ],
        'limits': {
            'maxFeaturedEvents': Event.MAX_FEATURED,
            'maxTopEvents': TOP_EVENTS_MAX_LIMIT,
            'maxFeaturedVenues': MAX_FEATURED_VENUES,
        },
        'categories': [{'key': k, 'label': v} for k, v in CATEGORIES],
        'imageSpec': BANNER_IMAGE_SPEC,
        'today': today.isoformat(),
    }


def _admin_name(request):
    return getattr(request.user, 'name', '') or getattr(request.user, 'phone', '')


def _locked_settings():
    Settings.load()                      # the row exists before we lock it
    return Settings.objects.select_for_update().get(pk=1)


def _saved(settings_obj, fields):
    settings_obj.homepage_revision += 1
    settings_obj.public_version += 1
    settings_obj.save(update_fields=[*fields, 'homepage_revision', 'public_version'])


def _stale(settings_obj):
    return Response(
        {
            'detail': 'Someone else changed the homepage while you were editing. '
                      'The latest version has been loaded — check it and try again.',
            'code': 'STALE_HOMEPAGE',
            'homepage': admin_payload(settings_obj),
        },
        status=status.HTTP_409_CONFLICT,
    )


def _revision_ok(body, settings_obj):
    try:
        return int(body.get('revision')) == settings_obj.homepage_revision
    except (TypeError, ValueError):
        return False


class _HomepageAdminView(APIView):
    """Admin session + IsAdmin + JSON only (see adminpanel._AdminWriteView
    for why JSON-only is a CSRF control)."""

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [JSONParser]

    def _body(self, request):
        return request.data if isinstance(request.data, dict) else {}

    def _audit(self, request, action, target, change='', target_id=''):
        from .views import record_audit
        record_audit(request, action, target, change, target_id=target_id)


class AdminHomepageView(_HomepageAdminView):
    """GET /api/admin/homepage — the whole Homepage page in one read."""

    def get(self, request):
        return Response(admin_payload(Settings.load()))


class AdminHomepageSectionsView(_HomepageAdminView):
    """PUT /api/admin/homepage/sections {revision, sections:[{key, enabled,
    title, subtitle, limit?}, ...]} — the movable sections in their new
    order; the fixed three keep their places whatever the order sent."""

    def put(self, request):
        body = self._body(request)
        entries = body.get('sections')
        if not isinstance(entries, list) or not entries:
            return detail('sections must be a list.', status.HTTP_400_BAD_REQUEST)

        cleaned = []
        seen = set()
        for entry in entries:
            if isinstance(entry, dict) and entry.get('key') in RETIRED_SECTIONS:
                continue
            if not isinstance(entry, dict) or entry.get('key') not in SECTION_DEFAULTS:
                return detail('Unknown section in the list.', status.HTTP_400_BAD_REQUEST)
            key = entry['key']
            if key in seen:
                return detail(f'Section {key} is listed twice.', status.HTTP_400_BAD_REQUEST)
            seen.add(key)
            enabled = entry.get('enabled', True)
            if not isinstance(enabled, bool):
                return detail('enabled must be true or false.', status.HTTP_400_BAD_REQUEST)
            if key in REQUIRED_SECTIONS and not enabled:
                return detail(
                    f'“{SECTION_DEFAULTS[key]["label"]}” is the main venue list and always shows.',
                    status.HTTP_400_BAD_REQUEST,
                )
            title = entry.get('title', SECTION_DEFAULTS[key]['title'])
            subtitle = entry.get('subtitle', SECTION_DEFAULTS[key]['subtitle'])
            if not isinstance(title, str) or not isinstance(subtitle, str):
                return detail('Titles must be text.', status.HTTP_400_BAD_REQUEST)
            if len(title.strip()) > TITLE_MAX or len(subtitle.strip()) > SUBTITLE_MAX:
                return detail(
                    f'Titles can be {TITLE_MAX} characters and subtitles {SUBTITLE_MAX}.',
                    status.HTTP_400_BAD_REQUEST,
                )
            row = {'key': key, 'enabled': enabled, 'title': title.strip(), 'subtitle': subtitle.strip()}
            if key == 'topEvents' and 'limit' in entry:
                limit = entry['limit']
                if not isinstance(limit, int) or isinstance(limit, bool) \
                        or not 1 <= limit <= TOP_EVENTS_MAX_LIMIT:
                    return detail(
                        f'Show between 1 and {TOP_EVENTS_MAX_LIMIT} top events.',
                        status.HTTP_400_BAD_REQUEST,
                    )
                row['limit'] = limit
            cleaned.append(row)

        with transaction.atomic():
            settings_obj = _locked_settings()
            if not _revision_ok(body, settings_obj):
                return _stale(settings_obj)
            current = homepage_config(settings_obj)
            sent = {row['key']: row for row in cleaned}
            # The movable sections in the order sent; any the admin left out
            # keep their current place after them. The fixed three lead.
            movable = [row['key'] for row in cleaned if row['key'] in MOVABLE_SECTIONS]
            movable += [
                s['key'] for s in current['sections']
                if s['key'] in MOVABLE_SECTIONS and s['key'] not in movable
            ]
            merged = []
            for key in list(FIXED_SECTIONS) + movable:
                # A section left out of the request keeps what it had.
                source = sent.get(key) or _section(current, key)
                row = {k: source[k] for k in ('key', 'enabled', 'title', 'subtitle')}
                if key == 'topEvents':
                    row['limit'] = source.get('limit', _section(current, 'topEvents')['limit'])
                merged.append(row)
            homepage = dict(settings_obj.homepage or {})
            homepage['sections'] = merged
            settings_obj.homepage = homepage
            _saved(settings_obj, ['homepage'])

        on = [r['key'] for r in cleaned if r['enabled']]
        self._audit(request, 'Updated homepage sections', 'Homepage',
                    f'shown: {", ".join(on) or "none"}')
        return Response(admin_payload(settings_obj))


def _find(banners, wanted):
    for index, raw in enumerate(banners):
        if isinstance(raw, dict) and banner_id(raw, index) == str(wanted):
            return index
    return None


class AdminBannersView(_HomepageAdminView):
    """POST /api/admin/homepage/banners {fields…} — a new banner/promotion,
    appended last in both orders. New records start UNPUBLISHED unless the
    admin explicitly sends published: true, so they can be previewed first."""

    def post(self, request):
        body = dict(self._body(request))
        body.setdefault('published', False)
        counts = redemption_counts()
        with transaction.atomic():
            settings_obj = _locked_settings()
            existing = all_banners(settings_obj)
            try:
                banner = clean_banner(body, None, existing, counts)
            except BannerError as error:
                return Response({'detail': str(error), 'code': error.code},
                                status=error.http_status)
            now = timezone.now().isoformat()
            banner.update({
                'id': f'bn_{uuid.uuid4().hex[:12]}',
                'heroOrder': max([b['heroOrder'] for b in existing] + [-1]) + 1,
                'promoOrder': max([b['promoOrder'] for b in existing] + [-1]) + 1,
                'createdAt': now, 'updatedAt': now, 'updatedBy': _admin_name(request),
            })
            settings_obj.banners = [*(settings_obj.banners or []), banner]
            _saved(settings_obj, ['banners'])
        self._audit(request, 'Created homepage banner', banner['title'],
                    'published' if banner['published'] else 'draft', target_id=banner['id'])
        return Response(
            {'banner': banner, 'homepage': admin_payload(settings_obj)},
            status=status.HTTP_201_CREATED,
        )


class AdminBannerDetailView(_HomepageAdminView):
    """PATCH /api/admin/homepage/banners/<id> {fields…, expectedUpdatedAt}
    DELETE /api/admin/homepage/banners/<id> — only while no paid booking has
    used its code (otherwise unpublish it or end its dates)."""

    def patch(self, request, banner_ref):
        body = self._body(request)
        counts = redemption_counts()
        with transaction.atomic():
            settings_obj = _locked_settings()
            stored = list(settings_obj.banners or [])
            index = _find(stored, banner_ref)
            if index is None:
                return detail('Banner not found.', status.HTTP_404_NOT_FOUND)
            current = normalize_banner(stored[index], index)
            if 'expectedUpdatedAt' in body and \
                    str(body.get('expectedUpdatedAt') or '') != str(stored[index].get('updatedAt') or ''):
                return Response(
                    {
                        'detail': 'Another admin changed this banner after you opened it. '
                                  'Their version has been loaded — re-apply your change.',
                        'code': 'BANNER_CHANGED',
                        'homepage': admin_payload(settings_obj),
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            others = [b for i, b in enumerate(all_banners(settings_obj)) if i != index]
            try:
                banner = clean_banner(body, current, others, counts)
            except BannerError as error:
                return Response({'detail': str(error), 'code': error.code},
                                status=error.http_status)
            if not str(stored[index].get('id') or ''):
                banner['id'] = f'bn_{uuid.uuid4().hex[:12]}'
            else:
                banner['id'] = stored[index]['id']        # keep its original type
            banner['heroOrder'], banner['promoOrder'] = current['heroOrder'], current['promoOrder']
            banner['createdAt'] = stored[index].get('createdAt') or ''
            banner['updatedAt'] = _next_stamp(stored[index].get('updatedAt'))
            banner['updatedBy'] = _admin_name(request)
            stored[index] = banner
            settings_obj.banners = stored
            _saved(settings_obj, ['banners'])
        changed = sorted(k for k in body if k in _BANNER_FIELDS and body[k] != current.get(k))
        self._audit(request, 'Edited homepage banner', banner['title'],
                    ', '.join(changed) or 'no changes', target_id=str(banner['id']))
        return Response({'banner': banner, 'homepage': admin_payload(settings_obj)})

    def delete(self, request, banner_ref):
        counts = redemption_counts()
        with transaction.atomic():
            settings_obj = _locked_settings()
            stored = list(settings_obj.banners or [])
            index = _find(stored, banner_ref)
            if index is None:
                return detail('Banner not found.', status.HTTP_404_NOT_FOUND)
            current = normalize_banner(stored[index], index)
            used = counts.get(current['code'], 0) if current['code'] else 0
            if used:
                return Response(
                    {
                        'detail': f'Code {current["code"]} has been used on {used} paid '
                                  f'booking{"s" if used != 1 else ""}, so this promotion is kept '
                                  'as part of their history. Unpublish it, or set its '
                                  '“Valid to” date to end it.',
                        'code': 'PROMOTION_REDEEMED',
                        'redemptions': used,
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            del stored[index]
            settings_obj.banners = stored
            _saved(settings_obj, ['banners'])
        self._audit(request, 'Deleted homepage banner', current['title'],
                    current['code'] or 'no code', target_id=current['id'])
        return Response({'homepage': admin_payload(settings_obj)})


class AdminBannersReorderView(_HomepageAdminView):
    """POST /api/admin/homepage/banners/reorder {revision, placement:
    'hero'|'promotions', ids:[...]} — ids in their new order; banners not
    listed keep their relative order after them."""

    def post(self, request):
        body = self._body(request)
        placement = body.get('placement')
        field = {'hero': 'heroOrder', 'promotions': 'promoOrder'}.get(placement)
        ids = body.get('ids')
        if field is None:
            return detail('placement must be "hero" or "promotions".', status.HTTP_400_BAD_REQUEST)
        if not isinstance(ids, list) or not all(isinstance(i, (str, int)) for i in ids):
            return detail('ids must be a list.', status.HTTP_400_BAD_REQUEST)
        ids = [str(i) for i in ids]
        with transaction.atomic():
            settings_obj = _locked_settings()
            if not _revision_ok(body, settings_obj):
                return _stale(settings_obj)
            stored = list(settings_obj.banners or [])
            normalized = all_banners(settings_obj)
            known = {b['id'] for b in normalized}
            if any(i not in known for i in ids):
                return detail('One of those banners no longer exists — reload and try again.',
                              status.HTTP_400_BAD_REQUEST)
            rest = sorted((b for b in normalized if b['id'] not in ids), key=lambda b: b[field])
            ranking = {pk: rank for rank, pk in enumerate(ids + [b['id'] for b in rest])}
            for index, raw in enumerate(stored):
                if isinstance(raw, dict):
                    stored[index] = {**raw, field: ranking[banner_id(raw, index)]}
            settings_obj.banners = stored
            _saved(settings_obj, ['banners'])
        self._audit(request, f'Reordered {placement} banners', 'Homepage', f'{len(ids)} banners')
        return Response(admin_payload(settings_obj))


class AdminBannerImageView(APIView):
    """POST /api/admin/homepage/banner-image (multipart: image, slot) ->
    {url, width, height, warnings}.

    Checks the real bytes — format, size and pixel dimensions — against
    BANNER_IMAGE_SPEC before anything is stored. Same Supabase bucket and the
    same X-Requested-With requirement as AdminUploadView (see its docstring:
    the custom header is what forces a CORS preflight for a multipart POST).
    """

    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from venues.storage import ALLOWED_TYPES, StorageError, upload_photo
        from venues.views import _looks_like_image

        from .views import record_audit

        if not request.headers.get('X-Requested-With'):
            return detail('Uploads must send an X-Requested-With header.',
                          status.HTTP_400_BAD_REQUEST)
        slot = str(request.data.get('slot') or 'desktop')
        if slot not in BANNER_IMAGE_SPEC['slots']:
            return detail('slot must be "desktop" or "mobile".', status.HTTP_400_BAD_REQUEST)
        upload = request.FILES.get('image') or request.FILES.get('file')
        if upload is None:
            return detail('No file uploaded.', status.HTTP_400_BAD_REQUEST)
        extension = ALLOWED_TYPES.get(upload.content_type)
        if extension is None:
            return detail('Only JPEG, PNG or WebP images are allowed.', status.HTTP_400_BAD_REQUEST)
        if upload.size > BANNER_MAX_BYTES:
            return detail(
                f'Image is too large ({upload.size / (1024 * 1024):.1f} MB). '
                f'The limit is {BANNER_MAX_BYTES // (1024 * 1024)} MB — export it as a '
                'compressed JPEG or WebP.',
                status.HTTP_400_BAD_REQUEST,
            )
        if not _looks_like_image(upload):
            return detail('That file is not a valid JPEG, PNG or WebP image.',
                          status.HTTP_400_BAD_REQUEST)
        data = upload.read()
        size = image_size(data)
        if size is None:
            return detail('Could not read this image’s size. Re-save it as JPEG or PNG and try again.',
                          status.HTTP_400_BAD_REQUEST)
        width, height = size
        problem = image_problem(slot, width, height)
        if problem:
            return Response(
                {'detail': problem, 'code': 'IMAGE_DIMENSIONS', 'width': width, 'height': height},
                status=status.HTTP_400_BAD_REQUEST,
            )
        path = f'banners/{uuid.uuid4().hex}.{extension}'
        try:
            url = upload_photo(path, data, upload.content_type)
        except StorageError as error:
            logger.warning('Banner upload failed: %s', error)
            return detail('Could not store the image right now. Please try again.',
                          status.HTTP_502_BAD_GATEWAY)
        record_audit(request, 'Uploaded a banner image', upload.name or 'image',
                     f'{slot} {width}×{height}, {upload.size // 1024} KB')
        return Response(
            {'url': url, 'width': width, 'height': height, 'slot': slot,
             'warnings': ratio_warnings(slot, width, height)},
            status=status.HTTP_201_CREATED,
        )


def _schedule(body, event):
    """Apply optional from/until (YYYY-MM-DD or '') to the event. -> error."""
    for key, attr in (('from', 'featured_from'), ('until', 'featured_until')):
        if key not in body:
            continue
        raw = str(body.get(key) or '').strip()
        if not raw:
            setattr(event, attr, None)
            continue
        parsed = _parse_date(raw)
        if parsed is None:
            return f'"{key}" must be a date (YYYY-MM-DD).'
        setattr(event, attr, parsed)
    if event.featured_from and event.featured_until and event.featured_from > event.featured_until:
        return '“Show from” must be on or before “Show until”.'
    return None


class AdminFeaturedEventsView(_HomepageAdminView):
    """POST /api/admin/homepage/featured-events {eventId, from?, until?} —
    feature an EXISTING published event (the same promotion the Events page's
    Promote button sets, so the cap and the live-only rule are shared)."""

    def post(self, request):
        from events.admin_views import _set_featured
        from events.models import Event

        body = self._body(request)
        try:
            event_id = uuid.UUID(str(body.get('eventId') or ''))
        except ValueError:
            return detail('Pick an event.', status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            # Lock order everywhere: the Settings row, then events.
            settings_obj = _locked_settings()
            event = Event.objects.select_for_update().filter(pk=event_id).first()
            if event is None or event.status == Event.Status.DELETED:
                return detail('Event not found.', status.HTTP_404_NOT_FOUND)
            if not event.featured:
                error = _set_featured(event, True)
                if error:
                    transaction.set_rollback(True)
                    return error
            problem = _schedule(body, event)
            if problem:
                transaction.set_rollback(True)
                return detail(problem, status.HTTP_400_BAD_REQUEST)
            highest = Event.objects.filter(featured=True).exclude(pk=event.pk).aggregate(
                top=Max('featured_order')
            )['top']
            if event.featured_order is None:
                event.featured_order = (highest or 0) + 1
            event.save(update_fields=['featured_order', 'featured_from', 'featured_until', 'updated_at'])
            _saved(settings_obj, [])
        self._audit(request, 'Featured event on homepage', event.name,
                    _window_text(event), target_id=str(event.pk))
        return Response(admin_payload(settings_obj), status=status.HTTP_201_CREATED)


def _window_text(event):
    start = event.featured_from.isoformat() if event.featured_from else 'now'
    end = event.featured_until.isoformat() if event.featured_until else 'no end date'
    return f'shows {start} → {end}'


class AdminFeaturedEventDetailView(_HomepageAdminView):
    """PATCH …/featured-events/<id> {from?, until?} — reschedule.
    DELETE …/featured-events/<id> — stop featuring it. The event itself, its
    dates, tickets and orders are untouched."""

    def _event(self, event_id):
        from events.models import Event
        return Event.objects.select_for_update().filter(pk=event_id, featured=True).first()

    def patch(self, request, event_id):
        body = self._body(request)
        with transaction.atomic():
            settings_obj = _locked_settings()
            event = self._event(event_id)
            if event is None:
                return detail('That event is not featured.', status.HTTP_404_NOT_FOUND)
            problem = _schedule(body, event)
            if problem:
                return detail(problem, status.HTTP_400_BAD_REQUEST)
            event.save(update_fields=['featured_from', 'featured_until', 'updated_at'])
            _saved(settings_obj, [])
        self._audit(request, 'Rescheduled featured event', event.name,
                    _window_text(event), target_id=str(event.pk))
        return Response(admin_payload(settings_obj))

    def delete(self, request, event_id):
        from events.admin_views import _set_featured

        with transaction.atomic():
            settings_obj = _locked_settings()
            event = self._event(event_id)
            if event is None:
                return detail('That event is not featured.', status.HTTP_404_NOT_FOUND)
            _set_featured(event, False)
            _saved(settings_obj, [])
        self._audit(request, 'Removed event from homepage', event.name,
                    'event kept; homepage feature off', target_id=str(event.pk))
        return Response(admin_payload(settings_obj))


class AdminFeaturedEventsReorderView(_HomepageAdminView):
    """POST …/featured-events/reorder {revision, ids:[...]}"""

    def post(self, request):
        from events.models import Event

        body = self._body(request)
        ids = body.get('ids')
        if not isinstance(ids, list):
            return detail('ids must be a list.', status.HTTP_400_BAD_REQUEST)
        try:
            ids = [uuid.UUID(str(i)) for i in ids]
        except ValueError:
            return detail('ids must be event ids.', status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            settings_obj = _locked_settings()
            if not _revision_ok(body, settings_obj):
                return _stale(settings_obj)
            featured = {
                e.pk: e for e in Event.objects.select_for_update().filter(featured=True)
            }
            if any(pk not in featured for pk in ids):
                return detail('One of those events is no longer featured — reload and try again.',
                              status.HTTP_400_BAD_REQUEST)
            for rank, pk in enumerate(ids, start=1):
                Event.objects.filter(pk=pk).update(featured_order=rank)
            # Events not in the list follow, keeping their relative order.
            rest = sorted(
                (e for pk, e in featured.items() if pk not in ids),
                key=lambda e: (e.featured_order is None, e.featured_order or 0),
            )
            for rank, event in enumerate(rest, start=len(ids) + 1):
                Event.objects.filter(pk=event.pk).update(featured_order=rank)
            _saved(settings_obj, [])
        self._audit(request, 'Reordered featured events', 'Homepage', f'{len(ids)} events')
        return Response(admin_payload(settings_obj))


class AdminFeaturedVenuesReorderView(_HomepageAdminView):
    """POST …/featured-venues/reorder {revision, ids:[...]} — the order of
    admin-featured venues in Spotlight. Featuring itself stays the existing
    PATCH /api/admin/venues/<id> {featured} switch."""

    def post(self, request):
        from venues.models import Listing

        body = self._body(request)
        ids = body.get('ids')
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            return detail('ids must be a list.', status.HTTP_400_BAD_REQUEST)
        try:
            parsed = [uuid.UUID(i) for i in ids]
        except ValueError:
            return detail('ids must be venue ids.', status.HTTP_400_BAD_REQUEST)
        if Listing.objects.filter(pk__in=parsed, featured=True).count() != len(set(parsed)):
            return detail('One of those venues is no longer featured — reload and try again.',
                          status.HTTP_400_BAD_REQUEST)
        with transaction.atomic():
            settings_obj = _locked_settings()
            if not _revision_ok(body, settings_obj):
                return _stale(settings_obj)
            homepage = dict(settings_obj.homepage or {})
            homepage['featuredVenueOrder'] = [str(pk) for pk in parsed]
            settings_obj.homepage = homepage
            _saved(settings_obj, ['homepage'])
        self._audit(request, 'Reordered featured venues', 'Homepage', f'{len(ids)} venues')
        return Response(admin_payload(settings_obj))
