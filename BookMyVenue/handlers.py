"""
Custom error views (P5b).

When a URL matches no route — e.g. a malformed, non-UUID id like
`/api/venues/not-a-uuid/availability` — Django would normally return its HTML
error page. These handlers return the frontend's JSON error shape instead.

Wired up via `handler404` / `handler500` in BookMyVenue/urls.py. They only take
effect when DEBUG=False (production and the test runner).
"""
from django.http import JsonResponse


def not_found(request, exception=None):
    return JsonResponse({'message': 'Not found'}, status=404)


def server_error(request):
    return JsonResponse({'message': 'Something went wrong.'}, status=500)
