"""
Super-admin management of custom sub-categories (Sub-categories page, and
the Register Venues form's option lists).

    GET   /api/admin/subcategories?status=&category=   every name, any status
    POST  /api/admin/subcategories   {category, name}  add one — approved at once
    PATCH /api/admin/subcategories/<id>  {status: approved|rejected|pending, reason?}

Vendors add names through the registration wizard's "Others" option; those
wait here as PENDING. Every rule (tidying, duplicates, which categories take
custom names) is venues/subcategories.py — this file is only the admin's
door to it. Every write is audited.
"""
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from venues.models import CustomSubCategory
from venues.subcategories import (
    SUPPORTED_CATEGORIES,
    SubCategoryError,
    builtin_match,
    category_for_type,
    clean_name,
    ensure,
    invalidate,
    name_key,
    row_payload,
)

from .auth import detail
from .views import _AdminWriteView, record_audit

_STATUSES = {choice for choice, _ in CustomSubCategory.Status.choices}


def _admin_name(request):
    return getattr(request.user, 'name', '') or getattr(request.user, 'phone', '') or 'admin'


class AdminSubCategoriesView(_AdminWriteView):
    def get(self, request):
        rows = CustomSubCategory.objects.select_related('created_by')
        wanted = request.query_params.get('status')
        if wanted:
            if wanted not in _STATUSES:
                return detail('status must be pending, approved or rejected.', status.HTTP_400_BAD_REQUEST)
            rows = rows.filter(status=wanted)
        category = request.query_params.get('category')
        if category:
            rows = rows.filter(category=category_for_type(category) or category)
        return Response({
            'subCategories': [row_payload(r, for_admin=True) for r in rows.order_by('category', 'name')[:1000]],
            'categories': list(SUPPORTED_CATEGORIES),
            'counts': {
                s: CustomSubCategory.objects.filter(status=s).count() for s in sorted(_STATUSES)
            },
        })

    def post(self, request):
        body = self._body(request)
        category = category_for_type(body.get('category'))
        if not category:
            return detail(
                'Choose Private Hall, Sports, Gaming, Fitness or Big Screen Surprise.',
                status.HTTP_400_BAD_REQUEST,
            )
        try:
            name = clean_name(body.get('name'))
        except SubCategoryError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)
        builtin = builtin_match(category, name)
        if builtin:
            return Response(
                {'detail': f'"{builtin}" is already a built-in {category} sub-category.', 'code': 'BUILT_IN'},
                status=status.HTTP_409_CONFLICT,
            )
        existing = CustomSubCategory.objects.filter(category=category, name_key=name_key(name)).first()
        if existing is not None and existing.status == CustomSubCategory.Status.APPROVED:
            return Response(
                {'detail': f'"{existing.name}" already exists under {category}.', 'code': 'DUPLICATE',
                 'subCategory': row_payload(existing, for_admin=True)},
                status=status.HTTP_409_CONFLICT,
            )
        row, created = ensure(
            category, name, user=request.user, by_admin=True, reviewer=_admin_name(request),
        )
        record_audit(
            request, 'Added sub-category' if created else 'Approved sub-category',
            f'{category} → {row.name}', 'created approved' if created else f'was {existing.status}',
            target_id=str(row.pk),
        )
        return Response(
            {'subCategory': row_payload(row, for_admin=True)},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class AdminSubCategoryDetailView(_AdminWriteView):
    def patch(self, request, subcategory_id):
        row = CustomSubCategory.objects.select_related('created_by').filter(pk=subcategory_id).first()
        if row is None:
            return detail('Sub-category not found.', status.HTTP_404_NOT_FOUND)
        body = self._body(request)
        new_status = str(body.get('status') or '').strip()
        if new_status not in _STATUSES:
            return detail('status must be pending, approved or rejected.', status.HTTP_400_BAD_REQUEST)
        before = row.status
        if new_status != before:
            row.status = new_status
            row.reviewed_by = _admin_name(request)
            row.reviewed_at = timezone.now()
            row.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
            invalidate()
            record_audit(
                request, 'Updated sub-category', f'{row.category} → {row.name}',
                f'status: {before} → {new_status}', target_id=str(row.pk),
                reason=body.get('reason'),
            )
        return Response({'subCategory': row_payload(row, for_admin=True)})
