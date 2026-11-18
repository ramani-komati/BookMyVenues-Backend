"""
Global error formatter.

The frontend contract says every non-2xx response body looks like:
    {"message": "human readable reason"}

DRF's built-in errors (bad token -> 401, throttled -> 429, etc.) use
{"detail": "..."} instead — this handler renames that key so the
frontend never has to special-case anything.
"""
from rest_framework.views import exception_handler


def _first_text(value):
    """The first human-readable string inside a DRF error structure."""
    if isinstance(value, dict):
        for item in value.values():
            text = _first_text(item)
            if text:
                return text
        return ''
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _first_text(item)
            if text:
                return text
        return ''
    return str(value or '')


def api_exception_handler(exc, context):
    """Every error body carries ONE human-readable sentence under the key its
    client reads — "message" (customer/vendor API) or "detail" (/api/admin)
    — plus "code" when DRF knows one. Field validation errors
    ({"phone": ["…"]}) keep their per-field map under "errors" (QA audit
    2026-10-30: those had no message at all)."""
    response = exception_handler(exc, context)
    if response is None:
        return response

    request = context.get('request') if context else None
    key = 'detail' if request is not None and request.path.startswith('/api/admin') else 'message'
    data = response.data
    codes = getattr(exc, 'get_codes', lambda: None)()

    if isinstance(data, dict) and 'detail' in data and len(data) == 1:
        body = {key: str(data['detail'])}
        if isinstance(codes, dict) and isinstance(codes.get('detail'), str):
            body['code'] = codes['detail']
        elif isinstance(codes, str):
            body['code'] = codes
    elif isinstance(data, (dict, list)):
        body = {key: _first_text(data) or 'The request could not be processed.',
                'errors': data, 'code': 'invalid'}
    else:
        body = {key: str(data)}
    response.data = body
    return response
