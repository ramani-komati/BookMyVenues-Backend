"""
Global error formatter.

The frontend contract says every non-2xx response body looks like:
    {"message": "human readable reason"}

DRF's built-in errors (bad token -> 401, throttled -> 429, etc.) use
{"detail": "..."} instead — this handler renames that key so the
frontend never has to special-case anything.
"""
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None and isinstance(response.data, dict):
        detail = response.data.get('detail')
        if detail is not None:
            response.data = {'message': str(detail)}

    return response
