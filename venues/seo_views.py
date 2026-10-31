"""
GET /api/sitemap.xml — the customer site's sitemap (QA audit 2026-10-30:
https://thebookmyvenues.in/sitemap.xml answered the app's HTML).

The customer site (Vercel) rewrites /sitemap.xml to this endpoint, so the
URLs inside point at the PUBLIC site (settings.PUBLIC_SITE_URL), never at
this API host. Only what a customer can open is listed: the public pages,
LIVE venues by their stable slug (unit siblings — a single court or screen
— are reached through their venue, so they are left out) and LIVE events.
Cached for an hour.
"""
from xml.sax.saxutils import escape

from django.conf import settings
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import cache_page

from events.models import Event

from .models import Listing

STATIC_PAGES = (
    ('/home', 'daily', '1.0'),
    ('/events', 'daily', '0.8'),
    ('/about-us', 'monthly', '0.3'),
    ('/contact-us', 'monthly', '0.3'),
    ('/privacy-policy', 'yearly', '0.1'),
    ('/terms-and-conditions', 'yearly', '0.1'),
    ('/cancellation-refund-policy', 'yearly', '0.1'),
)


def site_url():
    return str(getattr(settings, 'PUBLIC_SITE_URL', '') or 'https://thebookmyvenues.in').rstrip('/')


def _url(loc, lastmod=None, changefreq='weekly', priority='0.6'):
    parts = [f'<loc>{escape(loc)}</loc>']
    if lastmod is not None:
        parts.append(f'<lastmod>{lastmod.date().isoformat()}</lastmod>')
    parts.append(f'<changefreq>{changefreq}</changefreq>')
    parts.append(f'<priority>{priority}</priority>')
    return '<url>' + ''.join(parts) + '</url>'


def sitemap_xml():
    base = site_url()
    urls = [_url(base + path, None, freq, prio) for path, freq, prio in STATIC_PAGES]
    for slug, record, updated in (
        Listing.objects.filter(status=Listing.Status.LIVE).exclude(slug='')
        .order_by('slug').values_list('slug', 'record', 'updated_at')
    ):
        detail = (record or {}).get('detail') if isinstance(record, dict) else None
        if isinstance(detail, dict) and str(detail.get('unitOf') or '').strip():
            continue
        urls.append(_url(f'{base}/venue/{slug}', updated, 'weekly', '0.8'))
    for slug, updated in (
        Event.objects.filter(status=Event.Status.LIVE).exclude(slug='')
        .order_by('slug').values_list('slug', 'updated_at')
    ):
        urls.append(_url(f'{base}/events/{slug}', updated, 'daily', '0.7'))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + '\n'.join(urls) + '\n</urlset>\n')


@method_decorator(cache_page(60 * 60), name='get')
class SitemapView(View):
    def get(self, request):
        return HttpResponse(sitemap_xml(), content_type='application/xml; charset=utf-8')
