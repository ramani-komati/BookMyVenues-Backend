"""GET /api/subcategories — the APPROVED custom sub-categories per canonical
category ({categories: {'Play zone': ['Cricket Nets', …]}}), which the vendor
registration wizard offers next to the built-in ones. Public: they are shown
to customers anyway (GET /api/config). Pending / rejected names are never
listed here — the super admin sees those at /api/admin/subcategories."""
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .subcategories import approved_names


class PublicSubCategoriesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({'categories': approved_names()})
