# BookMyVenues — Complete API Reference

Production base URL: **`https://bookmyvenues-backend.onrender.com`**

There are **two APIs on this one backend**:

| App | Base | Auth | Error shape |
|-----|------|------|-------------|
| **Customer / Vendor** | `/api` | `Authorization: Bearer <JWT>` (valid **30 days**) | `{ "message": "..." }` |
| **Super-Admin** | `/api/admin` | **Cookie session** (`credentials: "include"`) | `{ "detail": "..." }` |

**Conventions**
- Content-Type `application/json` (except photo upload = `multipart/form-data`).
- On `401`, clear the session and re-login.
- Slots: `"HH:MM – HH:MM"` (24h, en dash or hyphen); end `"00:00"` = midnight.
  Business hours 06:00–24:00, 30-min steps, min 30 min.
- Numeric fields may be sent as strings (`"120"`) — the server coerces.
- Money: fields ending in `Num` are raw ints; `price`/`payout`/`stars` are display strings.
- Booking fee is a flat **₹20** per online booking (admin-configurable), never on walk-ins.

---
---

# PART A — Customer / Vendor API (`/api`)

## A0. Utility

### `GET /api/v1/health` — no auth
→ `{ "status": "ok" }`

### `GET /api/v1/auth/me` — Bearer
→ `{ "id", "name", "phone", "email", "role" }`

### `POST /api/v1/auth/refresh` — body `{ "refresh": "<token>" }`
→ `{ "access": "<jwt>" }`  *(access tokens last 30 days, so rarely needed.)*

---

## A1. Public browsing (no auth)

### `GET /api/venues?q=&category=&locality=&pincode=&page=&limit=&sort=`
`limit` ≤ 50 (default 20), `page` ≥ 1, `sort` = `popular` | `new` (default `new`). Only **live** venues. Cached ~60 s.
```json
{ "venues": [ { "id","name","category","locality","location","price","unit",
               "meta","image","gallery":[url],"status":"live","slug" } ], "total": 42 }
```

### `GET /api/venues/:idOrSlug` — full detail (UUID or slug)
Returns the full record + `"gallery":[url]` + `"detail": { description, amenities[], parking,
dining, capacity, packages[], sports[], addons[], offers[], occasions[], extraPersonPrice,
maxExtraPersons, contactPhone, address, mapsLink }`. `404` if not found / not live.

### `GET /api/venues/:id/availability?date=YYYY-MM-DD`  (`:id` = venue UUID)
Never cached. `400` bad/past date, `404` unknown venue.
```json
{ "date": "2026-08-01",
  "booked": ["19:00 – 20:00"],                                  // taken on EVERY unit
  "bookedUnits": [ { "sport":"Box Cricket","unit":1,"ranges":["19:00 – 20:00"] } ] }
```

### `GET /api/maps/resolve?url=<googleShortLink>` — no auth
Resolves `maps.app.goo.gl` / `goo.gl` / `g.co` share links. Any other host → `400`.
→ `{ "resolved": "https://www.google.com/maps/place/…!3d..!4d.." }`

---

## A2. Customer (`/api/users/...`)

### `POST /api/users/auth/otp` — `{ "phone":"9876543210" }` → `{ "sentTo":"9876543210" }`
`400` bad phone · `429` too many (3/phone/10 min) · `502` SMS down.
> OTP may arrive as a **voice call** (2Factor SMS route pending) — same code.

### `POST /api/users/auth/verify` — `{ "phone", "otp" }`  *(auto-creates the account)*
→ `{ "user": { "phone","name","email" }, "token":"<jwt>" }` · `401` wrong/expired.

### `PATCH /api/users/me` — Bearer · `{ "name"?, "email"? }`
Phone is immutable; email validated + unique. → `{ "user": { "phone","name","email" } }` · `400` invalid/dup.

### `GET /api/users/me/bookings?status=upcoming|past&page=&limit=` — Bearer
```json
{ "bookings": [ { "id":"bk_…","phone","customer","venueName","category","location","image",
  "date","slots":[],"sport","unit","unitLabel","perSlot","addons":[{"name","qty","price"}],
  "offer":{ "code","title","type","value" }|null,"discountAmount","amount",
  "method","walkIn","createdAt" } ], "total": 3 }
```

### `POST /api/users/me/bookings` — Bearer  *(confirm a booking)*
Body = booking record. Include `"venueId":"<uuid>"` (preferred over `venueName`).
Optional per-unit: `"sport"`, `"unit"` (1-based), `"unitLabel"`.
Optional coupon: `"offer": { "code":"SAVE10" }` (or `{ "code":"" }` for auto-apply) + your `"discountAmount"`.
The server **recomputes** everything:
```
base     = round(rate × min/60) + Σ(addon.price × qty)
discount = percent → min(round(base×value/100), maxDiscount?) | flat → min(value, base)
amount   = max(0, base − discount) + ₹20 fee          // fee never discounted
```
- `201 { "booking": { …record } }`
- `409` slot just taken · `400` validation / **amount mismatch** / bad offer · `401` · `404` venue gone

**Amount-mismatch error:** `{ "message":"Amount mismatch: expected ₹1820.", "code":"AMOUNT_MISMATCH", "expectedAmount":1820 }`

### `DELETE /api/users/me/bookings/:id` — Bearer (owner only)
→ `{ "cancelled": true, "id":"bk_…" }` · `404` not yours · `400` already past.

---

## A3. Vendor (`/api/vendors/...`)

### `POST /api/vendors/auth/otp` — `{ "phone" }` → `{ "sentTo" }`  (same rules as customer)

### `POST /api/vendors/auth/verify` — `{ "phone","otp" }`
- Returning → `{ "vendor": {…}, "isNew": false, "token":"<jwt>" }`
- New phone → `{ "vendor": null, "isNew": true }`  *(then call register within 30 min)*

### `POST /api/vendors` — `{ "phone","name","email" }`  *(phone must have just passed OTP)*
→ `201 { "vendor": {…}, "token":"<jwt>" }` · `400` · `403` not verified · `409` already a vendor.

### `PATCH /api/vendors/me` — Bearer · `{ "name"?, "email"? }`  (phone immutable)
→ `{ "vendor": { "phone","name","email" } }`

### `GET /api/vendors/me/dashboard` — Bearer  *(everything in one call)*
```json
{ "stats": { "today":{"value","trend"}, "slotsToday":{"value"}, "week":{…}, "month":{…} },
  "earnings": { "walkIn":{…}, "online":{…}, "total":{…} },
  "week": [ { "label":"Mon","value","online","walkIn" } ],
  "bookings": [ { "time","venue","customer","amount" } ],   // today, max 8
  "allBookings": [ …full booking records ],
  "venues": [ …listing summaries incl. gallery ] }
```

### `POST /api/vendors/me/walkin-bookings` — Bearer (own venue only)
`{ "venueId","date","slots":[],"customer","perSlot","amount" }` (+ optional `sport`/`unit`/`unitLabel`/`offer`).
`amount = round(perSlot × min/60) − discount` (**no ₹20 fee**). Same overlap rule → `409`.
→ `201 { "booking": { …, "walkIn": true, "method":"walk-in", "phone": null } }`

### `POST /api/vendors/me/listings` — Bearer  *(publish; idempotent by id)*
Body = listing record with `"id":"<draftId>"` + `gallery` + `detail` (incl. `offers`).
Resubmitting **updates**, never duplicates; an update with no `gallery` keeps existing photos.
→ `201 { "listing": { …record } }` · `400` bad id · `403` not your draft.

### `DELETE /api/vendors/me/listings/:id` — Bearer (owner only)
→ `{ "deleted": true, "id" }` · `409` if upcoming bookings exist · `404`.

---

## A4. Venue registration wizard (`/api/venues/drafts`, all Vendor Bearer)

Draft = 5 buckets `basics, location, details, payout, photos`. The 4 text buckets are stored
**verbatim** (so `details.offers`, `details.sports[].unitPrices`, etc. round-trip untouched).
Every write returns `completion` (0–100, steps of 20). Someone else's draftId → `404`.

| Method & path | Purpose |
|---|---|
| `POST /api/venues/drafts` | Create draft (body: any subset of the 4 text buckets) → `201 { draftId, draft, completion, savedAt }` |
| `GET /api/venues/drafts/:id` | Resume → `{ draftId, draft, completion, savedAt, status }` · `404` |
| `PATCH /api/venues/drafts/:id/sections/:section` | Autosave `basics\|location\|details\|payout`; body shallow-merged → `{ draftId, section, completion, savedAt }` |
| `POST /api/venues/drafts/:id/photos` | **multipart** `file` + `gallery`(`venuePhotos`\|`serviceImages`); JPEG/PNG/WebP ≤5 MB; caps 5/10 → `201 { …, photo:{ id,name,url } }` · `413` · `502` |
| `DELETE /api/venues/drafts/:id/photos/:photoId?gallery=…` | Remove a photo |
| `DELETE /api/venues/drafts/:id` | Clear the whole draft → `{ draftId, deleted:true }` |
| `POST /api/venues/drafts/:id/submit` | Gate-check → `{ draftId, status:"pending", submittedAt }` or `400 { "message":"Missing: …" }` |
| `POST /api/venues/drafts/:id/reopen` | Back to `draft` |
| `POST /api/venues/drafts/:id/seed` | Rebuild an editable draft **under the listing's id** (edit a live venue) |

**Offers** live inside `details.offers` (like `packages`/`addons`):
```json
{ "title","code","type":"percent|flat","value","minAmount","maxDiscount","expiry" }
```
Stored verbatim by the draft, the listing, and returned by public detail. Discounts are
applied & validated at booking time (see `POST /users/me/bookings`).

---
---

# PART B — Super-Admin API (`/api/admin`)

**Cookie session**, so every request sends `credentials: "include"`. Errors are `{ "detail": "..." }`.
The admin site's origin must be whitelisted in the backend's `CORS_ALLOWED_ORIGINS`.

## B1. Auth (2-step: password → OTP)

### `POST /api/admin/auth/login` — `{ "email","password" }`
→ `{ "otpRequired": true }` (sends an SMS OTP to the admin's phone) · `400` bad creds · `502` SMS down.

### `POST /api/admin/auth/verify-otp` — `{ "email","otp" }`
→ `{ "token":"<sessionKey>" }` **and sets the session cookie** · `400 { "detail":"That code is not right." }`.

### `POST /api/admin/auth/logout` → `204` (clears the cookie).

## B2. Bootstrap (one aggregate read)

### `GET /api/admin/bootstrap` — admin session
→ `{ approvals[], venues[], vendors[], users[], bookings[], payouts[], reviews[], audit[], settings }`
(shapes below). `403 { "detail" }` if not an admin.

## B3. Writes (partial `PATCH`; each echoes the updated entity)

| Endpoint | Body |
|---|---|
| `PATCH /api/admin/approvals/:id` | `{ status:"approved\|changes\|rejected\|pending" }` · `{ checks:{…} }` · `{ notes }` · `{ timeline:[…] }` |
| `PATCH /api/admin/venues/:id` | `{ status:"live\|paused" }` (paused hides it publicly) · `{ featured:true }` |
| `PATCH /api/admin/vendors/:id` | `{ kyc:"verified\|pending\|rejected" }` · `{ acc:"active\|suspended" }` |
| `PATCH /api/admin/users/:id` | `{ status:"active\|blocked" }` |
| `PATCH /api/admin/bookings/:id` | `{ status:"refunded\|…" }` |
| `PATCH /api/admin/payouts/:id` | `{ status:"pending\|failed\|completed" }` |
| `POST /api/admin/reviews/:id/resolve` | `{ action:"keep" }` or `{ action:"remove", reason }` |
| `PUT /api/admin/settings` | full Settings object (below) — returns the saved Settings; **`fee` is the live booking fee** |
| `POST /api/admin/audit` | an AuditEntry (append). The server also auto-writes audit rows after every admin action. |

Bad value → `400 { "detail" }`; unknown id → `404 { "detail":"Not found" }`.

## B4. Entity shapes (`bootstrap` arrays)

```
Approval  { id, name, vendor, phone, category, city, area, submitted, waitingH,
            completion(0-100), status, price, capacity, packages, amenities[],
            payout, notes, checks:{photos,pricing,payout}, photo, timeline:[{label,time}] }
Venue     { id, name, vendor, category, city, area, price, rating, bookings,
            status:"live|paused|draft", featured, capacity, packages, hours,
            addedOn, revenueNum, amenities[], photo }
Vendor    { id, name, phone, email, venues, earningsNum, joined,
            kyc:"verified|pending|rejected", acc:"active|suspended", payout }
User      { id, name, phone, bookings, spentNum, lastActive, status:"active|blocked" }
Booking   { id, customer, venue, slot, amountNum, method:"UPI|Card|Cash",
            status:"confirmed|completed|refund_pending|refunded|cancelled",
            slotsDesc, slotsAmt, addons }
Payout    { id, vendor, period, grossNum, status:"pending|failed|completed" }
Review    { id, venue, reviewer, rating(1-5), text, reason, stars }
AuditEntry{ time, admin, action, target, change }
Settings  { fee, feeDate, commission, categories[], cities[], amenities[],
            banners:[{id,title,text}] }
```

**Note on ids:** `vendors/users/payouts/reviews` ids are numeric; **`venues`/`approvals`
ids are UUID strings** and **bookings are `"bk_…"`** (not `BMV-…`). The panel treats ids as
opaque and echoes them back in write URLs, so this works as-is.

---

## Admin setup — already done
- An admin account exists in production (email + password log in; OTP goes to the
  admin's phone). More admins: `python manage.py createsuperuser`.
- CORS is currently allow-all **and** credential-aware (the backend echoes the
  caller's origin), so the admin panel works from any URL today — no origin
  whitelisting needed until CORS is tightened for launch.
