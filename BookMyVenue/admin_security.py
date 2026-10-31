"""
Protection for the Django admin login (QA audit 2026-10-30: /admin/ was an
unprotected, guessable password form).

  * DJANGO_ADMIN_ENABLED=false  removes the Django admin entirely (the super
    admin panel does not use it).
  * DJANGO_ADMIN_PATH           moves it off the well-known /admin/ (default
    stays 'admin/' so nothing breaks until you choose a path).
  * Failed sign-ins are limited per client IP: after ADMIN_LOGIN_MAX_FAILURES
    (default 5) wrong passwords within ADMIN_LOGIN_LOCK_SECONDS (default
    15 min) the login answers 429 until the window passes. A successful
    sign-in clears the counter. Staff who are already signed in are never
    affected.
"""
import hashlib

from django.conf import settings
from django.contrib import admin
from django.core.cache import cache
from django.http import HttpResponse


def _client_ip(request):
    # Render / Vercel put the real client first in X-Forwarded-For.
    forwarded = str(request.META.get('HTTP_X_FORWARDED_FOR') or '').split(',')[0].strip()
    return forwarded or str(request.META.get('REMOTE_ADDR') or '')


def _key(request):
    return 'admin-login-fail:' + hashlib.sha256(_client_ip(request).encode()).hexdigest()


def max_failures():
    return int(getattr(settings, 'ADMIN_LOGIN_MAX_FAILURES', 5) or 5)


def lock_seconds():
    return int(getattr(settings, 'ADMIN_LOGIN_LOCK_SECONDS', 900) or 900)


_original_login = admin.site.login


def guarded_login(request, extra_context=None):
    key = _key(request)
    if request.method == 'POST' and (cache.get(key) or 0) >= max_failures():
        return HttpResponse(
            'Too many failed sign-in attempts. Try again later.',
            status=429, content_type='text/plain; charset=utf-8',
        )
    response = _original_login(request, extra_context)
    if request.method == 'POST':
        if request.user.is_authenticated and request.user.is_staff:
            cache.delete(key)
        else:
            if not cache.add(key, 1, lock_seconds()):
                try:
                    cache.incr(key)
                except ValueError:
                    cache.set(key, 1, lock_seconds())
    return response


def install():
    """Wrap the admin login once (idempotent)."""
    if getattr(admin.site, '_bmv_guarded', False):
        return
    admin.site.login = guarded_login
    admin.site._bmv_guarded = True


def admin_path():
    path = str(getattr(settings, 'DJANGO_ADMIN_PATH', 'admin/') or 'admin/').strip().strip('/')
    return f'{path}/'


def admin_enabled():
    return bool(getattr(settings, 'DJANGO_ADMIN_ENABLED', True))
