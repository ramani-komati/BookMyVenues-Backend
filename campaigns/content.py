"""
Campaign content: field rules, and rendering the email itself.

The admin writes plain text — no HTML is ever accepted from the panel. The
body supports just enough formatting for a promotion and is escaped before
any of it is applied:

    blank line      new paragraph
    line break      line break
    **words**       bold
    - item          bullet list (consecutive lines)
    {{first_name}}  the customer's first name ("there" when unknown)

The SAME renderer serves the panel preview, the test email and the real send,
so what the admin previews is exactly what customers receive.
"""
import html as html_lib
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.text import slugify

LIMITS = {
    'name': 120, 'subject': 150, 'previewText': 200, 'heading': 150,
    'body': 5000, 'ctaLabel': 40, 'ctaUrl': 500,
}
MAX_IMAGES = 6
IMAGE_TEXT_LIMITS = {'alt': 150, 'caption': 300, 'link': 500}

FIRST_NAME_TOKEN = '{{first_name}}'
# Stand-ins swapped for each recipient's values after ONE template render.
_NAME_SLOT = '__BMV_FIRST_NAME__'
_UNSUB_SLOT = '__BMV_UNSUBSCRIBE_URL__'

FIELD_MAP = {
    'name': 'name', 'subject': 'subject', 'previewText': 'preview_text',
    'heading': 'heading', 'body': 'body', 'ctaLabel': 'cta_label', 'ctaUrl': 'cta_url',
}


class ContentError(ValueError):
    """A field the admin must fix; `field` names it for the form."""

    def __init__(self, message, field=''):
        super().__init__(message)
        self.field = field


def _site():
    return str(settings.BRAND_SITE_URL or '').rstrip('/')


def normalize_url(raw, field, *, allow_relative=True):
    """https URL, or a site path ('/venues/x') made absolute on the customer
    site. '' stays ''. Anything else (http, javascript:, mailto:) is refused —
    an email link is a promise to the reader about where it goes."""
    value = str(raw or '').strip()
    if not value:
        return ''
    if allow_relative and value.startswith('/') and not value.startswith('//'):
        value = _site() + value
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.netloc or any(c.isspace() for c in value):
        raise ContentError('Links must start with https:// (or be a site path like /venues).', field)
    return value


def clean_images(raw):
    if raw in (None, ''):
        return []
    if not isinstance(raw, list):
        raise ContentError('images must be a list.', 'images')
    if len(raw) > MAX_IMAGES:
        raise ContentError(f'A campaign can carry at most {MAX_IMAGES} images.', 'images')
    images = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContentError('Each image must be an object.', 'images')
        url = normalize_url(item.get('url'), 'images', allow_relative=False)
        if not url:
            raise ContentError(f'Image {index + 1} has no URL.', 'images')
        image = {'url': url}
        for key, limit in IMAGE_TEXT_LIMITS.items():
            value = str(item.get(key) or '').strip()
            if len(value) > limit:
                raise ContentError(f'Image {index + 1}: {key} is longer than {limit} characters.', 'images')
            image[key] = value
        image['link'] = normalize_url(image['link'], 'images')
        images.append(image)
    return images


def apply_fields(campaign, body):
    """Copy the editable fields present in `body` onto the campaign.
    Lenient on completeness (it is a draft) but strict on shape and length."""
    for key, attr in FIELD_MAP.items():
        if key not in body:
            continue
        value = str(body.get(key) or '').strip()
        if len(value) > LIMITS[key]:
            raise ContentError(f'{key} is longer than {LIMITS[key]} characters.', key)
        if key == 'name' and not value:
            raise ContentError('Give the campaign a name.', 'name')
        if key == 'ctaUrl':
            value = normalize_url(value, 'ctaUrl')
        setattr(campaign, attr, value)
    if 'images' in body:
        campaign.images = clean_images(body.get('images'))


def send_problems(campaign):
    """{field: message} for what still blocks sending (tests included)."""
    problems = {}
    if not campaign.subject.strip():
        problems['subject'] = 'Add an email subject.'
    if not campaign.heading.strip():
        problems['heading'] = 'Add a heading.'
    if not campaign.body.strip() and not campaign.images:
        problems['body'] = 'Add some text or at least one image.'
    if bool(campaign.cta_label.strip()) != bool(campaign.cta_url.strip()):
        problems['ctaUrl' if campaign.cta_label.strip() else 'ctaLabel'] = (
            'A button needs both its text and its link.'
        )
    return problems


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _inline(text):
    """Escape, then apply **bold**. The name slot survives escaping intact."""
    escaped = html_lib.escape(text, quote=False).replace(FIRST_NAME_TOKEN, _NAME_SLOT)
    return _BOLD.sub(r'<strong>\1</strong>', escaped)


def body_blocks(body):
    """Body text -> [('p', html) | ('ul', [html, ...])] for the template."""
    blocks = []
    for chunk in re.split(r'\n\s*\n', str(body or '').replace('\r\n', '\n').strip()):
        lines = [line.rstrip() for line in chunk.split('\n') if line.strip()]
        if not lines:
            continue
        if all(line.lstrip().startswith(('- ', '• ')) for line in lines):
            blocks.append(('ul', [_inline(line.lstrip()[2:].strip()) for line in lines]))
        else:
            blocks.append(('p', '<br>'.join(_inline(line) for line in lines)))
    return blocks


def _plain(text):
    return _BOLD.sub(r'\1', str(text or '')).replace(FIRST_NAME_TOKEN, _NAME_SLOT)


def tracked(url, campaign):
    """Add utm tags to links that point at OUR site, so the site's analytics
    can tell which campaign brought a visit. Other links are left alone."""
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.netloc != urlparse(_site()).netloc:
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault('utm_source', 'email')
    query.setdefault('utm_medium', 'campaign')
    query.setdefault('utm_campaign', slugify(campaign.name)[:60] or 'campaign')
    return urlunparse(parsed._replace(query=urlencode(query)))


def render_template(campaign, *, is_test=False):
    """(subject, html, text) with the recipient's name and unsubscribe link
    still as placeholders — personalize() fills them per recipient. One
    render per batch instead of one per customer."""
    site = _site()
    images = [
        {**image, 'link': tracked(image.get('link'), campaign),
         'caption_html': _inline(image.get('caption') or '')}
        for image in (campaign.images or [])
    ]
    context = {
        'subject': campaign.subject.replace(FIRST_NAME_TOKEN, _NAME_SLOT),
        'preview_text': campaign.preview_text.replace(FIRST_NAME_TOKEN, _NAME_SLOT),
        'heading_html': _inline(campaign.heading),
        'blocks': body_blocks(campaign.body),
        'banner': images[0] if images else None,
        'gallery': images[1:],
        'cta_label': campaign.cta_label,
        'cta_url': tracked(campaign.cta_url, campaign),
        'logo_url': settings.BRAND_LOGO_URL,
        'site_url': site,
        'site_domain': urlparse(site).netloc or site,
        'postal_address': getattr(settings, 'CAMPAIGN_POSTAL_ADDRESS', ''),
        'unsubscribe_url': _UNSUB_SLOT,
        'is_test': is_test,
    }
    html = render_to_string('emails/campaign.html', context)

    lines = [_plain(campaign.heading), '']
    if campaign.body.strip():
        lines += [_plain(campaign.body), '']
    for image in images:
        if image.get('caption'):
            lines.append(_plain(image['caption']))
        if image.get('link'):
            lines.append(image['link'])
    if campaign.cta_label and campaign.cta_url:
        lines += ['', f'{campaign.cta_label}: {context["cta_url"]}']
    lines += [
        '', '—',
        'You are receiving this because you chose to get offers from TheBookMyVenues.',
        f'Unsubscribe: {_UNSUB_SLOT}',
        'Booking confirmations and sign-in codes are not affected.',
    ]
    if context['postal_address']:
        lines.append(context['postal_address'])
    subject = campaign.subject.replace(FIRST_NAME_TOKEN, _NAME_SLOT)
    if is_test:
        subject = f'[TEST] {subject}'
    return subject, html, '\n'.join(lines).strip()


def first_name(name):
    first = str(name or '').strip().split(' ')[0]
    return first or 'there'


def personalize(rendered, *, name, unsubscribe_url):
    """Fill one recipient's values into a render_template() result."""
    subject, html, text = rendered
    display = first_name(name)
    escaped = html_lib.escape(display)
    url_html = html_lib.escape(unsubscribe_url)
    return (
        subject.replace(_NAME_SLOT, display),
        html.replace(_NAME_SLOT, escaped).replace(_UNSUB_SLOT, url_html),
        text.replace(_NAME_SLOT, display).replace(_UNSUB_SLOT, unsubscribe_url),
    )
