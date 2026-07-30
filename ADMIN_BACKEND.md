# Super-admin backend — build status

Response to the frontend's *Super-Admin PENDING Backend APIs* spec.
Base `/api/admin` · **cookie session** auth · error shape `{ "detail": "..." }`.

**Last updated:** after Phase 3 — **admin backend complete** · Production: `https://bookmyvenues-backend.onrender.com`

---

## Status at a glance

| Area | Status |
|------|--------|
| Auth — `login` → `verify-otp` → `logout` (session cookie) | ✅ **DONE** |
| `GET /bootstrap` — one aggregate read | ✅ **DONE** |
| Writes — approvals / venues / vendors / users / bookings | ✅ **DONE** |
| Settings (`PUT`, drives the live fee) · Payouts · Reviews · Audit | ✅ **DONE** |

**Every endpoint in the super-admin spec now exists.** 🎉

---

## ✅ Phase 1 — auth + bootstrap read

### Auth (2-step, cookie session)
- **`POST /api/admin/auth/login`** `{ email, password }` → `{ "otpRequired": true }`.
  Validates an **ADMIN** user's email + password, then sends a 6-digit OTP by
  **SMS (2Factor)** to that admin's phone. Wrong email *or* password → `400`
  (same message, never reveals which). SMS down → `502`.
- **`POST /api/admin/auth/verify-otp`** `{ email, otp }` → `{ "token": "<sessionKey>" }`
  **and sets the session cookie**. Wrong/expired code → `400 { "detail": "That code is not right." }`.
- **`POST /api/admin/auth/logout`** → `204`, clears the cookie.

### `GET /api/admin/bootstrap` (admin session required)
Returns `{ approvals, venues, vendors, users, bookings, payouts, reviews, audit, settings }`.
Data comes from the real models where we have it:

| Key | Source | Notes |
|-----|--------|-------|
| `venues` | `Listing` | `rating` 0, `featured` false, status `live`/`draft` until Phase 2 |
| `vendors` | `User` (VENDOR) | `kyc` `"pending"` placeholder until Phase 2 |
| `users` | `User` (PUBLIC) | `status` from `is_active` |
| `bookings` | `Booking` | `method` UPI/Cash from walk-in flag; `status` confirmed/completed by date |
| `approvals` | pending `VenueDraft` | checklist/notes/timeline are Phase-2 defaults |
| `payouts` / `reviews` / `audit` | — | `[]` (no models yet, Phase 3) |
| `settings` | default | real `fee: "20"`; rest are defaults until Phase 3 |

---

## ✅ Phase 2 — write endpoints (partial PATCH, admin session required)

Each accepts a partial patch and echoes the updated entity (`200`).

- **`PATCH /api/admin/approvals/<id>`** — `{ status }` (pending/approved/changes/rejected),
  `{ checks: {...} }` (merged), `{ notes }`, `{ timeline: [...] }`. Persisted on the draft.
- **`PATCH /api/admin/venues/<id>`** — `{ status: "live"|"paused" }` (a paused venue is
  **hidden from public browsing**), `{ featured: true|false }`.
- **`PATCH /api/admin/vendors/<id>`** — `{ kyc: "verified"|"pending"|"rejected" }`,
  `{ acc: "active"|"suspended" }` (suspend flips the account off).
- **`PATCH /api/admin/users/<id>`** — `{ status: "active"|"blocked" }`.
- **`PATCH /api/admin/bookings/<id>`** — `{ status: "refunded" }` (any of
  confirmed/completed/refund_pending/refunded/cancelled).

Bad values → `400 { "detail": ... }`; unknown id → `404 { "detail": "..." }`.

New columns (all additive migrations, existing rows unaffected): `Listing.featured`
+ `paused` status; `VenueDraft.review_status/checks/notes/timeline`; `User.kyc`
(vendor suspend/user block reuse `is_active`); `Booking.status`.

---

## ✅ Phase 3 — new models (settings / payouts / reviews / audit)

- **`PUT /api/admin/settings`** — body is the full Settings object; saves and
  returns it. **The `fee` field is now the live booking fee** — changing it here
  changes what customers are charged (default ₹20). Also stores commission,
  categories, cities, amenities, banners.
- **`PATCH /api/admin/payouts/<id>`** — `{ status: "pending"|"failed"|"completed" }`
  (process / retry). Payouts are created by the payments system (future); the
  panel lists and updates them.
- **`POST /api/admin/reviews/<id>/resolve`** — `{ action: "keep" }` or
  `{ action: "remove", reason }`. Removed reviews drop out of the panel's queue.
- **`POST /api/admin/audit`** — append an audit row (panel-written).
  **The server ALSO writes its own audit row after every admin write** (approval,
  venue, vendor, user, booking, payout, review, settings), so the audit log is
  populated even without the frontend posting.

`bootstrap` now returns real `payouts`, flagged `reviews`, recent `audit`, and
the saved `settings`. New tables via `adminpanel/0001_initial` (Settings /
Payout / Review / AuditEntry).

---

## ✅ Setup — done

1. **Admin account exists** in production (created 30 Jul 2026): login is
   email + password, OTP goes to the admin's phone. Add more admins later with
   `python manage.py createsuperuser`.
2. **CORS:** allow-all is currently ON and works with the cookie login — with
   credentials enabled the backend echoes the caller's exact origin instead of
   `*`, so the panel works from any URL today. **When tightening later**, set
   `CORS_ALLOW_ALL_ORIGINS=False` and list both frontends' exact origins in
   `CORS_ALLOWED_ORIGINS`.
3. **Cookies:** cross-site sessions need HTTPS (production has it) and
   `SameSite=None` (already set for production).

## Deviations for the frontend to note

- **IDs differ from the mock's assumptions:** venues & approvals use **UUID
  strings** (not ints); bookings use `"bk_..."` (not `"BMV-..."`); vendors &
  users are ints. The panel treats ids as opaque keys, so this is fine as long
  as it echoes them back in write URLs.
- A few display fields are **placeholders** until Phase 2/3 (ratings, KYC,
  featured, paused, payouts, reviews, audit, most settings).

---

## 🎉 Admin backend complete

Every endpoint in the super-admin spec is built, tested, and live. Test-first,
pushed to `main` (Render auto-deploys). **Tests: 174 passing.**

Left for the frontend/product side (not backend blockers): reviews & payouts
have no *creation* path yet (they appear once the review-submission and
payment-run features exist); the panel's numeric-id assumption differs from our
UUID/`bk_` ids (see deviations above).
