"""
Excel (.xlsx) exports for the vendor dashboard and the super-admin panel.

    workbook.py        the one workbook writer every export uses
    common.py          shared filter parsing and money/booking helpers
    admin_reports.py   super-admin exports   (GET /api/admin/exports/<kind>)
    vendor_reports.py  vendor exports        (GET /api/vendors/me/exports/<kind>)
    views.py           the two endpoints: auth, permission, throttle, audit

Every export is built on the server from the same records the dashboard
reads, with the page's own filters applied there — the browser never receives
a bigger table to sift. Vendor exports are scoped to the token's account in
the query itself. Read-only throughout: no booking, payment, approval or
account state is ever changed by an export.

The super admin's main Bookings export predates this package and stays where
it is (adminpanel/exports.py); it writes its file through workbook.py too.
"""
