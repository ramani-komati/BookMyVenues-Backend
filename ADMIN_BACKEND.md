# Super-admin backend — build status

Response to the frontend's *Super-Admin PENDING Backend APIs* spec.
Base `/api/admin` · **cookie session** auth · error shape `{ "detail": "..." }`.

**Last updated:** after Phase 1 · Production: `https://bookmyvenues-backend.onrender.com`

---

## Status at a glance

| Area | Status |
|------|--------|
| Auth — `login` → `verify-otp` → `logout` (session cookie) | ✅ **DONE** |
| `GET /bootstrap` — one aggregate read | ✅ **DONE** (real data where it exists) |
| Writes — approvals / venues / vendors / users / bookings / payouts / reviews | ⏳ Phase 2 |
| New models — Settings (real fee), Payouts, Reviews, Audit | ⏳ Phase 3 |

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

## ⚠️ Setup notes (important)

1. **Create an admin account** on the server:
   `python manage.py createsuperuser` — it asks for phone, name, email, password.
   The **phone** is where the login OTP is sent; the **email + password** are the
   login credentials.
2. **CORS:** the admin panel is a different site using cookies, so its exact
   origin **must be listed in `CORS_ALLOWED_ORIGINS`** (env var on Render).
   Browsers refuse credentialed requests against a wildcard origin, so the
   panel's cookie login will NOT work while `CORS_ALLOW_ALL_ORIGINS=True`.
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

## Planned next

- **Phase 2 — writes on existing entities:** approval workflow
  (approve/changes/reject/reopen, checklist, notes, timeline), venue
  paused/featured, vendor KYC/suspend, user block, booking refund. Adds a few
  columns to existing models.
- **Phase 3 — new models:** Settings (persist + wire the real fee), Payouts,
  Reviews, Audit log (server-side audit rows).

Test-first, pushed to `main` (Render auto-deploys). Tests: 156 passing.
