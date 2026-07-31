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

## ✅ Integration round 2 (items 1–10 from the panel team)

1. **Approval workflow** — publishing is now gated: a NEW listing is created
   `pending` (status is server-owned; the client's status field is ignored) and
   appears in `bootstrap → approvals[]` with real data incl. `photos[]`.
   Republishing an existing listing keeps its current status — approved venues
   never fall back to pending (this also grandfathers every venue that was live
   before this change). `PATCH /api/admin/approvals/:id` (id = listing id):
   `approved` → the venue AND its unit siblings (`detail.unitOf` family) go
   live; `rejected`/`changes` keep the family off the public catalogue while
   the vendor still sees it (with status) in their dashboard. A new sibling of
   an already-live base goes live directly. Public endpoints serve `live` only.
2. **Customer + vendor per phone** — customer identity is a flag, independent
   of role. Customer OTP login and booking creation set it; vendor registration
   never clears it. `users[]` = all customer identities (vendors who book
   included); `vendors[]` unchanged. Backfilled for existing data.
3. **Bootstrap completeness** — `venues[]` now excludes unit siblings server-
   side (counts match real venues); no LIMIT on any bootstrap array.
4. **Payouts** — generated automatically on every bootstrap, one row per vendor
   per completed week (Mon–Sun): `Σ(booking.amount − ₹20)` over ONLINE bookings;
   walk-ins and refunded/cancelled excluded. Idempotent — processed rows are
   never regenerated or reset. Rows carry `periodStart`/`periodEnd` (ISO).
5. **Settings fee** — was already live (the booking recompute reads it).
   `commission` removed from Settings responses (fee is the only revenue).
6. **CSRF** — admin writes are CSRF-exempt (no header needed); session cookie
   is `SameSite=None; Secure` in production. Confirmed.
7. **Blocked users enforced** — a blocked phone gets `403` on OTP request AND
   verify (no SMS sent); existing JWTs also stop working (auth layer rejects
   inactive accounts).
8. **Machine-readable dates** — bookings now carry `date` + `createdAt` (ISO);
   payouts carry `periodStart`/`periodEnd` (ISO).
9. **Audit rows** — `target` = entity NAME, new `targetId` field carries the id;
   the acting admin's display name was already stored.
10. **SMS notifications** — the backend does NOT send SMS on approval decisions;
    please change the panel wording. (2Factor's SMS route is still DLT-pending,
    so approval texts would arrive as voice calls — will revisit once approved.)

## ✅ Integration round 3 (31 Jul)

1. **Approval gating** — was already deployed; the crickBuzz repro happened in
   the 12-minute deploy window (published 22:10 UTC, gate live 22:22 UTC).
   crickBuzz was routed through the queue and approved via the panel —
   acceptance flow verified end-to-end in production.
2. **Time-based booking completion** — admin `bookings[]` rows flip
   `confirmed → completed` once the LAST SLOT ENDS (IST), computed at read
   time; explicit states (refunded etc.) always win. Rows now also carry the
   raw `slots` array (plus the existing ISO `date`/`createdAt`).
   `DELETE /users/me/bookings/:id` now refuses once the last slot has ended —
   time-based, enforced server-side.
3. **Payouts confirmed NET of the fee** — verified in production:
   Thalla 20–26 Jul raw ₹5,378 (2 bookings) → stored ₹5,338 = Σ(amount − 20).
   `periodStart`/`periodEnd` ISO already present on every row.

---

## 🎉 Admin backend complete

Every endpoint in the super-admin spec is built, tested, and live. Test-first,
pushed to `main` (Render auto-deploys). **Tests: 174 passing.**

Left for the frontend/product side (not backend blockers): reviews & payouts
have no *creation* path yet (they appear once the review-submission and
payment-run features exist); the panel's numeric-id assumption differs from our
UUID/`bk_` ids (see deviations above).
