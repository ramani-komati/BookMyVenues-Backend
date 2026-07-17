"""
Supabase Storage client — used by the photo upload/delete endpoints.

Files live in one bucket; each object's public URL is permanent, which
is what the frontend contract requires ("url must be persistent").
"""
import requests
from django.conf import settings

TIMEOUT = 20  # seconds to wait for Supabase before giving up

# Allowed image types -> file extension we store them under.
ALLOWED_TYPES = {
    'image/jpeg': 'jpg',
    'image/png': 'png',
    'image/webp': 'webp',
}

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB


class StorageError(Exception):
    """Raised when Supabase Storage can't be reached or rejects a request."""


def _headers(content_type=None):
    # Supabase wants the key in BOTH headers.
    headers = {
        'Authorization': f'Bearer {settings.SUPABASE_SERVICE_KEY}',
        'apikey': settings.SUPABASE_SERVICE_KEY,
    }
    if content_type:
        headers['Content-Type'] = content_type
    return headers


def public_url(path: str) -> str:
    return (
        f'{settings.SUPABASE_URL}/storage/v1/object/public/'
        f'{settings.SUPABASE_STORAGE_BUCKET}/{path}'
    )


def upload_photo(path: str, content: bytes, content_type: str) -> str:
    """Upload one image; returns its permanent public URL."""
    if not settings.SUPABASE_SERVICE_KEY:
        raise StorageError('Storage is not configured (SUPABASE_SERVICE_KEY missing).')

    url = (
        f'{settings.SUPABASE_URL}/storage/v1/object/'
        f'{settings.SUPABASE_STORAGE_BUCKET}/{path}'
    )
    try:
        response = requests.post(
            url, data=content, headers=_headers(content_type), timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        raise StorageError('Could not reach the storage service.') from exc

    if response.status_code not in (200, 201):
        raise StorageError(f'Storage upload failed (HTTP {response.status_code}).')

    return public_url(path)


def delete_photo(path: str) -> None:
    """Best-effort delete. Raises StorageError only on network failure —
    a 404 from storage is fine (file already gone)."""
    url = (
        f'{settings.SUPABASE_URL}/storage/v1/object/'
        f'{settings.SUPABASE_STORAGE_BUCKET}/{path}'
    )
    try:
        requests.delete(url, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise StorageError('Could not reach the storage service.') from exc
