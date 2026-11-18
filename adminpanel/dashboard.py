"""
GET /api/admin/dashboard?range=today|yesterday|7d|30d|month|all|custom
                         &from=YYYY-MM-DD&to=YYYY-MM-DD

The super admin Dashboard's money and booking figures for one period, venue
bookings and event ticket orders together. Read-only. Every number comes from
reports/finance.py — see its docstring for what each one means.
"""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from reports import finance

from .auth import CsrfExemptSessionAuthentication, IsAdmin, detail


class AdminDashboardView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]

    def get(self, request):
        params = request.query_params
        try:
            period = finance.resolve_range(
                params.get('range'), params.get('from'), params.get('to'),
            )
        except finance.RangeError as exc:
            return detail(str(exc), status.HTTP_400_BAD_REQUEST)
        response = Response(finance.dashboard(period))
        response['Cache-Control'] = 'no-store'   # live money figures
        return response
