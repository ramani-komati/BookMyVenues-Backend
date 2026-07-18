# Book My Venue — Backend API

Production base URL: **`https://bookmyvenues-backend.onrender.com/api`**

Set in the frontend: `VITE_API_BASE_URL=https://bookmyvenues-backend.onrender.com/api` and flip the `USE_MOCK` flags.

- **Auth:** phone + OTP → JWT. Send it as `Authorization: Bearer <token>` (valid 1 day; on 401 clear the session and re-login).
- **Content type:** `application/json` — except photo upload (`multipart/form-data`).
- **Error shape (every non-2xx):** `{ "message": "human readable reason" }`
- **Slots:** `"HH:MM – HH:MM"` (24h; en dash or plain hyphen both accepted), end `"00:00"` = midnight. Business hours 06:00–24:00, 30-min steps, min 30 min.
- **Rate limits:** auth endpoints 10/min per IP; OTP max 3 per phone per 10 min (429).
- Numeric fields may be sent as strings ("120") — the server coerces.

Health check (no auth): `GET https://bookmyvenues-backend.onrender.com/api/v1/health` → `{"status":"ok"}`

---

## Group 1 — Public browsing (no auth)

### 1.1 List venues
`GET /venues?q=&category=&locality=&pincode=&page=&limit=&sort=`

- `limit` ≤ 50 (default 20), `sort` = `popular` | `new` (default `new`). Bad params → 400.
- Only `live` venues. Responses cached ~60 s.

```json
{ "venues": [ { "id", "name", "category", "locality", "location", "price", "unit", "meta", "image", "status": "live", "slug" } ], "total": 42 }
```

### 1.2 Venue detail
`GET /venues/:idOrSlug` — accepts the UUID **or** the slug (`grand-palace-hall`).

Returns the full listing record: all summary fields **plus** `"gallery": [urls]` and `"detail": { description, amenities[], parking, dining, capacity, packages[], sports[], addons[], occasions[], extraPersonPrice, maxExtraPersons, contactPhone, address, mapsLink }`.

404 if not found or not live.

### 1.3 Availability
`GET /venues/:id/availability?date=YYYY-MM-DD` (`:id` = venue UUID)

```json
{ "date": "2026-07-21", "booked": ["19:30 – 21:00", "21:30 – 23:30"] }
```
Sorted by start time. Never cached. 400 bad/past date, 404 unknown venue.

---

## Group 2 — Customer (`/users/...`)

### 2.1 Send OTP
`POST /users/auth/otp` `{ "phone": "9876543210" }` → `{ "sentTo": "9876543210" }`

400 invalid phone · 429 too many requests · 502 SMS service down.
> Note: OTP may currently arrive as a **voice call** (2Factor account SMS route pending approval). Same code either way.

### 2.2 Verify OTP (login — auto-creates the account if new)
`POST /users/auth/verify` `{ "phone": "9876543210", "otp": "123456" }`

```json
{ "user": { "phone", "name", "email" }, "token": "jwt" }
```
401 wrong/expired OTP · 429 attempts exceeded (max 5 wrong tries; OTP lives 5 min, single-use).

### 2.3 My bookings
`GET /users/me/bookings?status=upcoming|past&page=&limit=` (User JWT)

```json
{ "bookings": [ { "id": "bk_…", "phone", "customer", "venueName", "category", "location", "image", "date", "slots": [], "perSlot", "addons": [{ "name","qty","price" }], "amount", "method", "walkIn", "createdAt" } ], "total": 3 }
```

### 2.4 Create booking
`POST /users/me/bookings` (User JWT)

Body = booking record **plus one extra field: `"venueId": "<listing uuid>"`** (please include it — `venueName` alone is ambiguous if two venues share a name). Server generates `id`, `createdAt`.

Server re-validates EVERYTHING: venue live, date/time not past (IST), slot rules, **no overlap**, and **amount** = `round(hourlyRate × minutes/60)` + add-ons (prices taken from the listing, not the request) + ₹20 fee. Mismatched amount → 400 `"Amount mismatch: expected ₹X."`

- 201 `{ "booking": { …record } }`
- **409** — slot just taken (two simultaneous requests: exactly one wins)
- 400 validation · 401 no token · 404 venue gone

### 2.5 Cancel booking
`DELETE /users/me/bookings/:id` (User JWT, owner-only)

→ `{ "cancelled": true, "id": "bk_…" }` · 404 not yours/unknown · 400 already completed (past date). Freed slots reappear in availability instantly.

---

## Group 3 — Vendor portal (`/vendors/...`)

### 3.1 Send OTP
`POST /vendors/auth/otp` `{ "phone" }` → `{ "sentTo" }` (same rules as 2.1)

### 3.2 Verify OTP
`POST /vendors/auth/verify` `{ "phone", "otp" }`

- Returning vendor → `{ "vendor": { "phone","name","email" }, "isNew": false, "token": "jwt" }`
- Unknown phone → `{ "vendor": null, "isNew": true }` (no token — show the signup form, then call 3.3 within 30 min)

### 3.3 Register vendor
`POST /vendors` `{ "phone", "name", "email" }` (phone must have just passed OTP)

→ 201 `{ "vendor": {…}, "token": "jwt" }` · 400 name/email invalid · 403 phone not OTP-verified · 409 already a vendor.

### 3.4 Dashboard
`GET /vendors/me/dashboard` (Vendor JWT) — everything in one call:

```json
{
  "stats": { "today": {"value","trend"}, "slotsToday": {"value"}, "week": {"value","trend"}, "month": {"value","trend"} },
  "earnings": { "walkIn": {"today","week","month"}, "online": {…}, "total": {…} },
  "week": [ { "label": "Mon", "value", "online", "walkIn" } ],
  "bookings": [ { "time", "venue", "customer", "amount" } ],
  "allBookings": [ …full booking records, newest first ],
  "venues": [ …listing summaries ]
}
```
`trend` = % change vs the previous period. `week` = last 7 days, oldest→newest. `bookings` = today's, max 8. Scoped to the token owner's venues only.

### 3.5 Publish listing
`POST /vendors/me/listings` (Vendor JWT)

Body = listing record with `"id": "<draftId>"` + `gallery` + `detail`. **Idempotent by id** — resubmitting updates, never duplicates. If an update has no `gallery`, existing photos are kept. Status comes back `"live"` (auto-approve; switches to `"pending"` when admin review ships).

→ 201 `{ "listing": { …record } }` · 400 bad/missing id · 403 not your draft/listing.

### 3.6 Delete listing
`DELETE /vendors/me/listings/:id` (Vendor JWT, owner-only)

→ `{ "deleted": true, "id" }` · **409 if the venue has upcoming bookings** (cancel them first) · 404 not yours. Past bookings keep rendering for customers (history preserved).

### 3.7 Walk-in booking
`POST /vendors/me/walkin-bookings` (Vendor JWT, own venue only)

```json
{ "venueName": "…", "venueId": "<uuid, preferred>", "date": "2026-07-21", "slots": ["21:30 – 23:30"], "customer": "Walk-in name", "perSlot": 600, "amount": 1200 }
```
`amount` must equal `round(perSlot × minutes/60)` — **no ₹20 fee** for walk-ins. Same overlap rule as 2.4 (→ 409).

→ 201 `{ "booking": { …record, "walkIn": true, "method": "walk-in", "phone": null } }`

---

## Group 4 — Venue registration wizard (`/venues/drafts`, all Vendor JWT)

Draft = 5 buckets: `basics, location, details, payout, photos`. The server stores the 4 text buckets **verbatim** — whatever the wizard saves is exactly what it gets back. Every write returns server-owned `completion` (0–100, steps of 20: one bucket = 20%).

Someone else's draftId → 404, always.

### 4.1 Create draft
`POST /venues/drafts` — body optional: any subset of the 4 text buckets.

→ 201 `{ "draftId": "<uuid>", "draft": { 5 buckets }, "completion", "savedAt" }`

### 4.2 Get draft (resume)
`GET /venues/drafts/:draftId` → `{ "draftId", "draft", "completion", "savedAt", "status": "draft"|"pending" }` · 404 → start a fresh draft.

### 4.3 Autosave a section
`PATCH /venues/drafts/:draftId/sections/:section` — `section` ∈ `basics|location|details|payout`. Body = that section's object; **shallow-merged** server-side (only sent keys overwritten).

Format checks only on present, non-empty values: phone 10d, pincode 6d, email, IFSC, PAN, UPI, account number 9–18d. Empty/partial fields never error (autosave-friendly).

→ `{ "draftId", "section", "completion", "savedAt" }` · 400 unknown section / bad format.

### 4.4 Upload photo
`POST /venues/drafts/:draftId/photos` — **multipart**: `file` (image) + `gallery` (`venuePhotos`|`serviceImages`).

Rules: JPEG/PNG/WebP only · ≤ 5 MB (413) · caps: venuePhotos ≤ 5, serviceImages ≤ 10. Stored on Supabase Storage; the returned `url` is permanent.

→ 201 `{ "draftId", "gallery", "photo": { "id", "name", "url" }, "completion", "savedAt" }` · 502 storage briefly down.

### 4.5 Delete photo
`DELETE /venues/drafts/:draftId/photos/:photoId?gallery=venuePhotos|serviceImages`

→ `{ "draftId", "gallery", "photoId", "completion", "savedAt" }`

### 4.6 Clear draft
`DELETE /venues/drafts/:draftId` → `{ "draftId", "deleted": true }`

### 4.7 Submit draft
`POST /venues/drafts/:draftId/submit`

Gates: basics `venueName`+`phone` · location `houseStreet`+`pincode`+`stateName`+`mapsLink` · ≥1 venue photo · details `primaryCategory`+`capacity`+≥1 amenity + pricing (category containing "playzone" → ≥1 sport, else ≥1 package) · payout `accountHolder`,`bankName`,`accountNumber`,`ifsc`,`phone`.

- OK → `{ "draftId", "status": "pending", "submittedAt" }` (idempotent — resubmitting a pending draft just returns the same)
- Incomplete → 400 `{ "message": "Missing: Venue name, Pincode, …" }`

After submit succeeds, call **3.5** with the record to make it live.

### 4.8 Reopen draft
`POST /venues/drafts/:draftId/reopen` → `{ "draftId", "status": "draft" }`

### 4.9 Seed draft (edit a listing whose draft is gone)
`POST /venues/drafts/:draftId/seed` — `draftId` = **the listing's id**; body = any subset of the 4 text sections. Creates-or-updates the draft under that id so resubmit updates in place.

→ `{ "draftId", "status": "draft" }`

---

## Cross-cutting

1. `Authorization: Bearer <token>`; 401 = missing/expired → clear session, show sign-in.
2. Booking writes are transactional with a per-venue lock — concurrent overlapping requests: one wins, the other gets 409.
3. Timestamps ISO-8601; dates `YYYY-MM-DD`; times IST (venue local).
4. Numeric fields accepted as strings.
5. Amounts in whole ₹. Fee ₹20 per online booking (not walk-ins). Duration price = `round(hourlyRate × minutes/60)`.
6. Payout bucket required keys: `accountHolder`, `bankName`, `accountNumber`, `ifsc`, `phone` (`upi`/`pan` optional, validated when present).

## Local development

```bash
python -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill SECRET_KEY etc.
python manage.py migrate && python manage.py runserver
```

## Deploying on Render

Build `pip install -r requirements.txt && python manage.py collectstatic --noinput` · Start `gunicorn BookMyVenue.wsgi:application` · Pre-deploy `python manage.py migrate` · Env vars: see `.env.example` (+ `DEBUG=False`, `ALLOWED_HOSTS=<service>.onrender.com`, Supabase pooler `DATABASE_URL` port 6543).
