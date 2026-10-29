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

**Location (optional, never stored):** `&lat=&lng=` (the customer's point) adds
`"distanceKm"` to every row (`null` when the venue has no map pin). `sort=distance`
**orders** nearest first (unpinned venues last). `radius=<km>` (0–500) **excludes**
farther venues *and* unpinned ones. `lat`/`lng` must come together; `sort=distance`
and `radius` need them → otherwise `400`. Every row also carries
`"coordinates": { "lat", "lng" } | null` — the pin derived from the vendor's own maps
link (`venues/geo.py`), never guessed. `GET /api/events` takes the same
`lat`/`lng`/`radius`/`sort=distance`.

### `GET /api/venues/:idOrSlug` — full detail (UUID or slug)
Returns the full record + `"gallery":[url]` + `"detail": { description, amenities[], parking,
dining, capacity, packages[], sports[], addons[], offers[], occasions[], extraPersonPrice,
maxExtraPersons, contactPhone, address, mapsLink }` + `"coordinates"` (as above). `404` if not found / not live.

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

### `GET /api/geo/search?q=<place>` · `GET /api/geo/reverse?lat=&lng=` — no auth
The customer's manual location picker, proxied to the geocoder set by
`GEOCODER_PROVIDER` (`nominatim` | `google`; keys stay server-side). Throttled 20/min.
→ `{ "results": [ { "label","locality","city","lat","lng" } ] }` · `{ "result": { "label","locality","city" } | null }`.
**Off by default** → `503 { "message", "code":"GEOCODER_NOT_CONFIGURED" }`; provider failure → `502 GEOCODER_UNAVAILABLE`.

---

### `GET /api/homepage` — no auth  *(the Explore page's admin-curated sections, cached)*
→ `{ sections:[{key, enabled, title, subtitle, limit?}], hero:[banner], topEvents:[event card],
promotions:[banner], featuredVenues:[venue card], generatedAt }`. Published content only: hero
banners/promotions are the platform promotions (`Settings.banners`) that are published and inside
their dates (promotions also need a usable code); `topEvents` are LIVE, promoted, bookable events
inside their `featured_from/until` window, in the admin's order (section `limit`); `featuredVenues`
are LIVE featured venues (never unit siblings, never contact phones). `{area}` in a title is for the
client to fill. Cached per process, keyed on `Settings.homepage_revision`/`public_version` + the IST
date — admin writes, ratings and event/venue promotion changes invalidate it everywhere at once.
404 on older deployments (the customer app then falls back to `/banners` + `/events?featured=1`).

### `GET /api/venues/:id/reviews?limit=&offset=` · `GET /api/events/:id/reviews?limit=&offset=` — no auth
Written reviews (newest first, stars-only ratings are counted but not listed) for a LIVE venue set /
LIVE event → `{ reviews:[{id, stars, review, customerName:"Ravi K.", unitLabel, createdAt}], total,
rating, count }`. Hidden (moderated) ratings are excluded everywhere. Venue/event cards and detail
pages carry `rating` (average) + `ratingCount`; events send `rating: null, ratingCount: 0` when unrated.

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
  "method","walkIn","createdAt",
  "invoiceNumber":"INV-2627-000123","paidAt":"<ISO UTC>"|null,"paidAtSource":"gateway|checkout|webhook"|null } ],
  "total": 3 }
```
**Invoice number + payment time** (every authorised booking / event-order record — the
customer's own history, the vendor's dashboard / ticket buyers, the admin bootstrap; never a
public endpoint). `invoiceNumber` is issued once by the backend when the row is created
(`bookings/invoices.py`: `INV-<financial year>-<serial>`, one series for venue bookings and event
orders, unique in the database) and never changes; it does **not** mean the booking was paid.
`createdAt` is when the booking was made; `paidAt` is when its successful payment was recorded
(`bookings/payment_record.py`) — Razorpay's signed capture time (`gateway`), or this server's
verification time (`checkout` / `webhook`). `paidAt` is `null` for unpaid, failed, walk-in and
pay-at-venue rows and for payments made before it was recorded — it is never filled from
`createdAt`. The admin venue-booking record also carries `razorpayOrderId`.

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

### Ratings & reviews — Bearer (the booking's own customer)
`POST /api/venues/:venueId/ratings` `{ bookingId, stars:1-5, review? }` ·
`POST /api/events/:eventId/ratings` `{ orderId, stars:1-5, review? }` → `201 { rating, count }`.
Eligible only when the booking/ticket is **confirmed with a verified Razorpay payment** and has
**ended** (venue: last slot over, IST; event: show end time, overnight shows end the next day).
Not yours → `403`; wrong venue/event → `400`; not finished → `409 "Booking not completed yet"` /
`"Event not completed yet"`; unpaid / cancelled / failed / refund-pending / refunded / walk-in →
`409 { code:"NOT_ELIGIBLE" }`; second rating → `409 "Already rated"`; review over 1000 chars → `400`.
`GET /api/users/me/bookings` and `/users/me/event-orders` rows carry `ratingStatus`
(`eligible|rated|not_completed|not_eligible`), `userRating`, `userReview` — the app prompts on
`eligible` only.

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

### Booking verification / check-in — Bearer (vendor), throttled 60/min
Booking IDs travel in the **body**. The *gate* is the venue or event being checked:
`venueId` **or** `eventId`, which must be the vendor's own (else `404`).
- `GET /api/vendors/me/check-ins` → `{ "gates": [ { "kind":"venue|event","id","name","status" } ], "checkedInToday": 3 }`
- `POST /api/vendors/me/check-ins/verify` `{ "bookingId", "venueId"|"eventId" }` → `200 { "result": Verdict }` — never writes.
- `POST /api/vendors/me/check-ins` (same body) → `200 { "result": { "code":"CHECKED_IN", … } }` when admitted;
  otherwise `409 { "message", "code", "result": Verdict }`. Atomic (row lock + conditional update): a booking is admitted **once**.

**Booking ID:** every booking type — venue bookings of every category (online and
walk-in) and event ticket orders — is issued the same 6-character ID (`K7M2QX`; no
0/O/1/I/L, never all-hex), unique across all of them via the `BookingCode` registry
(`bookings/booking_codes.py`). Bookings made earlier keep their `bk_…` / `eo_…` IDs,
which still verify.

`Verdict = { code, valid, canCheckIn, title, message, booking }` where `code` is
`VALID` · `INVALID_ID` · `SHORT_REF` (an all-hex invoice ref like `BMV-3F8A92` — not an ID) ·
`WRONG_VENUE` (another vendor's booking → `booking: null`, nothing revealed) ·
`PAYMENT_INCOMPLETE` · `CANCELLED` · `REFUNDED` · `ALREADY_CHECKED_IN` ·
`WRONG_DATE` (entry only on the booking's own date, IST) · `INELIGIBLE`. An event
order is admitted as one party (`tickets`, `admits`). Booking records now include `"checkedInAt"`.

### `POST /api/vendors/me/listings` — Bearer  *(publish; idempotent by id)*
Body = listing record with `"id":"<draftId>"` + `gallery` + `detail` (incl. `offers`).
Resubmitting **updates**, never duplicates; an update with no `gallery` keeps existing photos.
→ `201 { "listing": { …record } }` · `400` bad id · `403` not your draft.

### `DELETE /api/vendors/me/listings/:id` — Bearer (owner only)
→ `{ "deleted": true, "id" }` · `409` if upcoming bookings exist · `404`.

### `GET /api/vendors/me/exports/:kind` — Bearer (vendor), throttled 20/min  *(Excel download)*
A real `.xlsx` (`Content-Disposition: attachment; filename="<prefix>_YYYY-MM-DD.xlsx"`,
`Cache-Control: no-store`), built from the caller's OWN records only (reports/vendor_reports.py).

| `:kind` | file prefix | query params | sheets |
|---|---|---|---|
| `bookings` | `vendor_bookings` | `venue` (own listing id; covers its unit listings), `status=all\|upcoming\|completed\|refund_pending`, `from`, `to` (booking date), `q` | Bookings — exactly the Bookings tab's rows |
| `earnings` | `vendor_earnings` | `venue`, `from`, `to` | Summary (gross · fees · promo top-ups · net · online vs at venue · refunds), By Venue, By Month, Bookings |
| `events` | `vendor_event_sales` | — | Events, Ticket Types, Dates, Orders |
| `venues` | `vendor_venues` | — | Venues |

Another vendor's (or an unknown) `venue` id → `400 { "message":"Unknown venue." }`; bad filter → `400`;
more than 50,000 rows → `400` (narrow the filters — never truncated); unknown kind → `404`.
Booking records now also carry `"listingId"` (the listing booked) for the dashboard's venue filter.

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
| `POST /api/venues/drafts/:id/photos` | **multipart** `file` + `gallery`(`venuePhotos`\|`serviceImages`); JPEG/PNG/WebP ≤5 MB, at least 400×300px (either orientation), ≤8000px a side (venues/image_specs.py); caps 5/10 → `201 { …, photo:{ id,name,url }, image:{ width,height,warnings[] } }` · `400` (too small/large) · `413` · `502`. The same pixel rules (and the additive `image` field) apply to `POST /api/vendors/me/event-images`, `…/events/:id/poster` and `POST /api/admin/uploads`. |
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

### Excel exports — admin session, each one audited
`GET /api/admin/bookings/export?type=&status=&date=&basis=&from=&to=&today=&q=` — the Bookings page
(adminpanel/exports.py). Every other page: `GET /api/admin/exports/:kind` (reports/admin_reports.py,
throttled 20/min per admin):

| `:kind` | file prefix | query params (the page's own filters) |
|---|---|---|
| `users` | `admin_users` | `q` (name/email/phone/id), `status=active\|blocked` |
| `vendors` | `admin_vendors` | `q`, `acc=active\|suspended`, `kyc=pending\|verified\|rejected` |
| `venues` | `admin_venues` | `scope=live\|all`, `status=live\|paused\|deletion_requested\|draft\|deleted` |
| `approvals` | `admin_approvals` | `status`, `category`, `city` — every page of the list |
| `events` | `admin_events` | `tab=all\|live\|promoted\|draft\|paused\|pending\|rejected` (sheets: Events, Ticket Types, Dates) |
| `payouts` | `admin_payouts` | `status=pending\|completed\|failed` |
| `refunds` | `admin_refunds` | `tab=requests\|paused\|history` |
| `payments` | `admin_payments` | `from`, `to` (day the booking was made, IST) — sheets: Summary, Venue Payments, Event Payments, Refunds |
| `audit` | `admin_audit_log` | — (the whole log, not just the latest 100) |

→ `.xlsx` attachment, `Cache-Control: no-store`. Not an admin → `403`; bad filter or more than
50,000 rows → `400 { "detail" }`; unknown kind → `404`. Users/vendors never include passwords,
tokens or bank details. Money follows the payment records: revenue counts only bookings that
still stand; captured = rows with a gateway payment; refunds use the recorded amount (the full
captured amount, labelled as such, when none was recorded).

### `POST /api/admin/check-ins/verify` — admin session · `{ "bookingId" }`
Looks up ANY venue booking or event order across all venues/events — **read-only**,
never checks anyone in. → `{ "found", "code", "title", "message", "eligibleToday",
"booking": { kind, id, status, displayStatus, venue/event, vendor, customer, phone,
amount, paidOnline, dueAtVenue, paymentId, refundAmount, refundReason, checkedInAt,
checkedInBy:{name,phone} } }`. Vendor tokens get `403`.

### Homepage content — admin session (adminpanel/homepage.py), each write audited
| Endpoint | Body / notes |
|---|---|
| `GET /api/admin/homepage` | everything the Homepage page edits: `revision`, `sections`, `banners` (+ `state`, `redemptions`, `issues`), `featuredEvents`, `featuredVenues`, `limits`, `categories`, `imageSpec` |
| `PUT /api/admin/homepage/sections` | `{ revision, sections:[{key, enabled, title, subtitle, limit?}] }` — `venues` can't be disabled; hero / topEvents / featuredPromotions stay first |
| `POST /api/admin/homepage/banners` | banner fields (below); starts **unpublished** unless `published:true` |
| `PATCH /api/admin/homepage/banners/:id` | changed fields + `expectedUpdatedAt` → `409 BANNER_CHANGED` if edited since |
| `DELETE /api/admin/homepage/banners/:id` | refused `409 PROMOTION_REDEEMED` once a paid booking used its code |
| `POST /api/admin/homepage/banners/reorder` | `{ revision, placement:"hero"\|"promotions", ids }` |
| `POST /api/admin/homepage/banner-image` | multipart `image`, `slot=desktop\|mobile` + `X-Requested-With` → `{ url, width, height, warnings }` |
| `POST /api/admin/homepage/featured-events` | `{ eventId, from?, until? }` — LIVE events only, cap `Event.MAX_FEATURED` (12) |
| `PATCH` / `DELETE /api/admin/homepage/featured-events/:id` | `{ from, until }` / stop featuring (the event is untouched) |
| `POST /api/admin/homepage/featured-events/reorder` · `…/featured-venues/reorder` | `{ revision, ids }` |

Banner fields (stored on the existing `Settings.banners` records, which checkout still validates
codes against): `title, text, type(none|complimentary|percent|flat), value, code, minAmount,
maxDiscount, perUserLimit, from, to, image, imageMobile, imageAlt, showText, showCode, ctaLabel,
ctaUrl ("/path" or https://), category(celebrate|sports|gaming|resorts|events|fitness), terms,
published, showInHero, featuredPromotion`. A published hero banner needs an `image`. A redeemed
code's `code`/`type` are locked. Whole-list writes with a stale `revision` → `409 STALE_HOMEPAGE`
with the latest `homepage`. Image rules (`imageSpec`): desktop 2400×600 recommended (4:1, min
1200×300), phone 1200×600 (2:1, min 600×300), JPEG/PNG/WebP ≤ 5 MB — derived from the hero CSS.

### Ratings moderation — admin session
`GET /api/admin/ratings?kind=venue|event|all&visibility=visible|hidden|all&written=1&q=&limit=&offset=`
→ `{ ratings:[…], total }` · `PATCH /api/admin/ratings/:id` `{ hidden:true, reason }` /
`{ hidden:false }` — hides/restores (never deletes or edits); averages and caches update at once.

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
