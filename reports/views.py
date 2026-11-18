"""
The two export endpoints. Both are GET (read-only), reuse the access control
of the dashboard they belong to, and are rate-limited per account:

  GET /api/admin/exports/<kind>        super admin — cookie session + IsAdmin,
                                       {"detail"} errors, every export audited
  GET /api/vendors/me/exports/<kind>   vendor — JWT + IsVendor, {"message"}
                                       errors, scoped to the token's account

The file is built on the server and sent as an attachment that is never
cached (Cache-Control: no-store).
"""
import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from adminpanel.auth import CsrfExemptSessionAuthentication, IsAdmin, detail
from venues.views import IsVendor

from . import admin_reports, common, vendor_reports
from .workbook import build, xlsx_response

logger = logging.getLogger(__name__)

TOO_MANY = (
    f'More than {common.MAX_ROWS:,} rows match. Narrow the filters '
    '(for example a date range) and export again.'
)


class AdminExportView(APIView):
    authentication_classes = [CsrfExemptSessionAuthentication]
    permission_classes = [IsAdmin]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'export'

    def get(self, request, kind):
        from adminpanel.views import record_audit

        report = admin_reports.REPORTS.get(kind)
        if report is None:
            return detail('Unknown export.', status.HTTP_404_NOT_FOUND)
        try:
            filters = report.parse(request.query_params)
            sheets, count = report.build(filters)
            content = build(sheets)
        except common.ExportFilterError as error:
            return detail(str(error), status.HTTP_400_BAD_REQUEST)
        except common.TooManyRows:
            return detail(TOO_MANY, status.HTTP_400_BAD_REQUEST)
        # Exports carry customer names and phone numbers — every one is on
        # the audit trail with the filters that shaped it.
        record_audit(
            request, f'Exported {report.label}', f'{count} {report.noun}',
            common.describe(filters),
        )
        return xlsx_response(content, common.filename(report.prefix))


class VendorExportView(APIView):
    permission_classes = [IsVendor]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'export'

    def get(self, request, kind):
        report = vendor_reports.REPORTS.get(kind)
        if report is None:
            return Response({'message': 'Unknown export.'}, status=status.HTTP_404_NOT_FOUND)
        try:
            filters = report.parse(request.query_params, request.user)
            sheets, count = report.build(filters, request.user)
            content = build(sheets)
        except common.ExportFilterError as error:
            return Response({'message': str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except common.TooManyRows:
            return Response({'message': TOO_MANY}, status=status.HTTP_400_BAD_REQUEST)
        logger.info('Vendor %s exported %s (%s %s)', request.user.id, kind, count, report.noun)
        return xlsx_response(content, common.filename(report.prefix))
