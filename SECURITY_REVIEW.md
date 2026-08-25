# Security Review — full codebase

Date: 2026-08-10 · Reviewed at commit `4209542` · 361 tests

Four independent reviews ran in parallel (auth/access control, money/payments,
public surface/data exposure, admin panel/infrastructure). Every finding below
was re-verified against the code before anything was changed; the exploitable
ones are reproduced as tests in `bookings/test_security.py` so they cannot come
back silently.

---

## CRITICAL — fixed

### 1. Anyone could book for free, and we paid the vendor for it
`POST /api/users/me/bookings` created a **confirmed** booking directly, with no
gateway step, for any `method` the client sent — including `online`, `upi`,
`card` and `netbanking`. It also *defaulted* to `online`, so simply omitting the
field produced a fully-paid booking that nobody had paid for.

`adminpanel/payouts.py` pays out every non-walk-in booking that isn't
cancelled/refunded/pending, so each forged booking became a real bank transfer
of `amount − fee` to the vendor. A customer who is also a vendor could pay
themselves.

**Fix:** the direct endpoint is pay-at-venue only and defaults to `venue`. Card
and UPI must go through `POST /api/payments/order`, where the booking is held
`payment_pending` until Razorpay confirms it.
Tests: `FreeOnlineBookingTests`.

### 2. Admin panel was CSRF-open
Admin writes are deliberately CSRF-exempt (decoupled SPA) and the session cookie
is `SameSite=None`, so protection rested entirely on the CORS allowlist. But
form-encoded and multipart POSTs are CORS-*simple*: the browser sends them
cross-site with cookies and never preflights, so the allowlist was never
consulted. A plain `<form>` on any website could remove reviews or forge audit
entries using a logged-in admin's session.

**Fix:** `_AdminWriteView.parser_classes = [JSONParser]`. JSON bodies always
preflight, so every admin write is now origin-checked.

### 3. Admin approval was bypassable by any vendor
`_resolve_publish_status` sent a new listing straight to LIVE when
`detail.unitOf` named a LIVE base — without checking who owned that base. Every
live venue's UUID is public, so any vendor could publish an arbitrary listing
naming someone else's approved venue and go live instantly with no review.

**Fix:** the fast path requires `base.vendor_id == request.user.id`, and
`unitOf` is validated for ownership at publish time — which also protects the
rating/favourite folding and the status cascades that trust it.

### 4. Public availability endpoint could be hung by one listing
`detail.sports[].units` is vendor-supplied JSON and was used directly as a loop
bound on the public, deliberately uncached availability endpoint. Setting it to
`10^12` pinned a worker per request.

**Fix:** `MAX_UNITS = 64` clamp. Test: `AvailabilityDoSTests`.

---

## HIGH — fixed

| Finding | Fix |
|---|---|
| Negative vendor prices (`unitPrices`, add-ons, `price`) drove any customer's total to ₹0, bypassing all coupon rules | `_to_int` refuses negatives — one choke point covering every price path. Test: `NegativePriceTests` |
| An unknown `sport` fell through to the venue's top-level price (absent ⇒ ₹0 on playzone venues) and got a different conflict key from real bookings for that pitch | Unknown sport / out-of-range unit is refused. Test: `UnitPricingIntegrityTests` |
| Admins could delete any live venue that never requested deletion — defeating the vendor-consent flow (admins here are also vendors, i.e. competitors) | Requires a pending `deletion_requested_at` |
| One admin could deactivate another via the vendor-suspend endpoint (`vendor_accounts_q()` matches `is_vendor`, which admins have); `is_active` gates *all* login | Targets with `role=ADMIN` are refused |
| `POST /api/admin/audit` took the `admin` name from the request body — an audit trail that lets one admin attribute actions to another proves nothing | Always `request.user` |
| Bank/payout keys could reach the public venue record (stored and served verbatim) | `_strip_private_keys` denylist at publish |
| Maps resolver only checked the *first* URL; every redirect hop was followed blindly and the final URL returned to an anonymous caller | Per-hop allowlist, `MAX_REDIRECTS = 5`. Test: `test_redirect_to_unexpected_host_is_refused` |
| `CORS_ALLOW_ALL_ORIGINS=True` in production only emitted a `warnings.warn` | Hard fail (`ImproperlyConfigured`) — verified production is already locked down before making it fail-closed |
| Refunds: read-check-refund-write with no row lock, and `refundAmount` unbounded | `select_for_update()` + `0 < refundAmount <= booking.amount` |
| No throttles on venue search, availability, or the maps resolver (each makes a blocking outbound call) | `public` 60/min, `maps` 20/min |

---

## MEDIUM / LOW — fixed

- Deactivated admins could still pass password+OTP login (`is_active` not filtered).
- Admin login leaked which emails exist via response timing (`check_password` skipped for unknown emails) — now constant-work.
- The raw session key was returned in the login response body, defeating `HttpOnly`.
- `_to_int(float('1e999'))` raised an uncaught `OverflowError` (500 instead of 400).
- Vendor-register email uniqueness was case-sensitive while profile update was not.
- Photo upload trusted the client's `Content-Type`; now verifies JPEG/PNG/WebP magic bytes.
- Django admin: privilege fields (`role`, `is_staff`, `is_superuser`) are read-only for non-superusers; payout bank details are masked.
- Audit entries are tagged `SELF-ACTION` when an admin acts on their own venue.

---

## Open — needs a decision, not a code change

### Django `/admin/` bypasses the OTP entirely
All three admin accounts have `is_staff`/`is_superuser`, and `/admin/` is mounted
at a predictable path with no rate limiting. Anyone with a **password alone** —
no OTP, no SMS — can log in there and edit bookings, roles and listings
directly, outside the `AuditEntry` trail (Django logs to its own `LogEntry`).

Hardening applied: privilege fields read-only for non-superusers, bank details
masked. The remaining options are to remove `/admin/` from production routing,
move it behind an IP allowlist/VPN, or add a second factor to its login.

### 30-day JWTs with no revocation
Access tokens live 30 days with no blacklist and no `/auth/logout`. A leaked
token stays valid for a month; the only remedy is deactivating the whole
account. Fixing it means installing `token_blacklist` and having the frontend
call a logout endpoint — a coordinated change.

### Orphaned Razorpay orders
`PaymentOrderView` creates the gateway order *before* taking the slot lock. If
the overlap check then 409s, the order still exists and is payable; a payment
against it hits the webhook, matches no booking, and is ignored — captured money
with no booking and no refund. Rare (requires losing the race after checkout
started) but real. Fixing it well needs an order-tracking table.

---

## Verified correct — no change needed

Recorded so future reviews don't re-litigate them:

- **Razorpay signature handling** — `hmac.compare_digest`, fails closed on an
  empty secret, webhook is the source of truth, amount cross-checked, refuses to
  revive cancelled/refunded bookings, idempotent on re-delivery.
- **OTP** — `secrets`-generated, hashed with `make_password`, 5-minute expiry,
  5-attempt lockout, single-use, purpose-scoped, never logged.
- **Slot race safety** — `select_for_update()` inside `transaction.atomic()` on
  all three booking-creation paths.
- **Frozen booking economics** — `amount`/`fee` are frozen at creation, so
  editing a listing cannot retroactively change past payouts.
- **Coupons** — expiry, `minAmount`, `maxDiscount` and non-positive discounts all
  enforced; `source: 'platform'` is server-set and cannot be forged by a vendor.
- **Photo storage paths** — every path segment is server-generated; no traversal.
- **Secrets** — all from env, `SECRET_KEY` crash-on-missing, `.env` git-ignored
  and never committed.
- **Error responses** — no stack traces or internal state leak regardless of DEBUG.
- **JWT freshness** — tokens carry only `user_id`; role and `is_active` are read
  from the DB per request, so blocking takes effect immediately.
