# Backend changes — response to `docs/BACKEND_ISSUES.md`

Living status board. Rewritten as each phase lands. Base `/api` · JWT · errors `{ "message": "..." }`.

**Last updated:** after Phase 4 — **all items done** · Production: `https://bookmyvenues-backend.onrender.com`

---

## Status at a glance

| # | Item | Status |
|---|------|--------|
| 🔴 **P1** | Booking accepts packages / extra-persons / custom add-ons | ✅ **DONE** |
| 🟠 **P3** | `gallery` in venue list + dashboard `venues[]` | ✅ **DONE** |
| 🟡 **P4** | `PATCH /users/me` + `PATCH /vendors/me` (profile edits) | ✅ **DONE** |
| 🟡 **P5a** | Structured amount-mismatch error (`code`, `expectedAmount`) | ✅ **DONE** |
| 🟡 **P5b** | JSON `{ "message" }` 404s for bad/non-UUID ids | ✅ **DONE** |
| ✅ **P5c** | Pagination for `GET /venues` | ✅ Already works (`?page=`) |
| 🟠 **P2** | `GET /maps/resolve` — resolve Google Maps short links | ✅ **DONE** |
| 🔴 **P6** | Per-unit bookings (multi-pitch / court / screen) | ✅ **DONE** |

---

## ✅ P1 — Bookings accept packages, extra-persons & custom add-ons

`POST /users/me/bookings` recomputes:

```
amount = round(hourlyRate × minutes / 60)     ← slot: from the listing (server-authoritative)
       + Σ(addon.price × addon.qty)           ← add-ons: taken from the REQUEST
       + ₹20 fee
```

Packages / extra-persons / custom add-ons folded into `addons` are accepted,
priced, persisted, and echoed. `Unknown add-on` is gone. Guards kept: name
required, `qty ≥ 1`, `price ≥ 0`.

> **Security note:** add-on prices are trusted from the request for now (no live
> payment yet). Revisit when real payments land.

---

## ✅ P3 — `gallery` in venue list responses

`GET /venues` and the dashboard's `venues[]` (3.4) now include
`"gallery": ["url", …]` on every item (always present, `[]` if the vendor added
none). The heavy `detail` block still stays out of list rows.

---

## ✅ P4 — Profile update endpoints

- **`PATCH /users/me`** (User JWT) · body `{ "name"?, "email"? }`
  → `200 { "user": { "phone", "name", "email" } }`
- **`PATCH /vendors/me`** (Vendor JWT) → `200 { "vendor": { … } }`

Rules: `phone` is **immutable** (never read from the body). `name`, if sent,
must be non-empty (trimmed). `email`, if non-empty, is format-checked and must be
unique (sending `""`/null clears it). Errors: `400` invalid/duplicate email or
empty name · `401` no token · `403` non-vendor calling `/vendors/me`.

---

## ✅ P5a — Structured amount-mismatch error

A wrong `amount` on `POST /users/me/bookings` **and**
`POST /vendors/me/walkin-bookings` now returns:

```json
{ "message": "Amount mismatch: expected ₹2119.", "code": "AMOUNT_MISMATCH", "expectedAmount": 2119 }
```

No more regexing the message text.

---

## ✅ P5b — JSON 404s for malformed ids

Any URL that matches no route (e.g. a non-UUID id like
`/venues/not-a-uuid/availability`) now returns `{ "message": "Not found" }` with
status `404` instead of Django's HTML error page. `500`s return
`{ "message": "Something went wrong." }`.

---

## ✅ P5c — Pagination already supported

`GET /venues?page=2&limit=20` already works (offset paging, `limit` 1–50,
default 20, response includes `total`). No backend change needed.

---

## ✅ P2 — Google Maps short-link resolver

- **`GET /maps/resolve?url=<shortlink>`** (public, no auth)
  → `200 { "resolved": "https://www.google.com/maps/place/…!3d..!4d.." }`

The server follows the redirect and returns the final URL; results are cached
30 days. **SSRF guard:** only `maps.app.goo.gl`, `goo.gl`, `g.co` are accepted
(exact host match, no subdomains) — any other host → `400`. Only the final URL
string is returned, never the fetched page body. `400` on a missing/unsupported
url or a redirect that fails to resolve.

---

## ✅ P6 — Per-unit bookings (multi-pitch / court / screen)

A venue with 3 box-cricket pitches can now take 3 concurrent bookings in the
same time range (before, one booking blocked the whole venue).

**Booking requests** (`POST /users/me/bookings`, `POST /vendors/me/walkin-bookings`)
now accept and echo:

```json
{ "sport": "Box Cricket", "unit": 2, "unitLabel": "Box Cricket · Pitch 2" }
```

- `sport` is `null` for non-playzone venues; `unit` is 1-based; both are echoed
  in every booking record (2.3, 3.4). `unit: null` = a whole-venue booking.
- **Per-unit rate:** the slot price uses the listing's rate for that unit —
  `detail.sports[name==sport].unitPrices[unit-1]` (playzone) or
  `detail.unitPrices[unit-1]` (hall/theatre), falling back to `price` for
  single-unit venues. A wrong rate → structured `AMOUNT_MISMATCH` 400.
- **Overlap** is now per `(venue, sport, unit)` — different pitches don't block
  each other. Legacy bookings with no `unit` still block every unit (safe).
- **Availability** (`GET /venues/:id/availability`) keeps the flat `booked`
  array (ranges taken on **every** unit, so single-unit clients are unaffected)
  and adds:

  ```json
  "bookedUnits": [ { "sport": "Box Cricket", "unit": 1, "ranges": ["19:00 – 20:00"] } ]
  ```

Migration `0003` adds three **nullable** columns — the existing bookings are
untouched and keep working as whole-venue bookings.

---

## ✅ Coupons / offers

Offers live inside a venue's `detail.offers` (like `packages`/`addons`):
`{ title, code, type: percent|flat, value, minAmount, maxDiscount, expiry }`.

- **Items 1–3 needed no code** — draft sections, the listing `record`, and the
  public detail response all store & echo the `detail` blob **verbatim**, so
  `detail.offers` rides along automatically (PATCH draft details → GET draft;
  publish → listing; `GET /venues/:idOrSlug`).
- **Item 4 — discount applied at booking** (`POST /users/me/bookings` and
  `/vendors/me/walkin-bookings`):

  ```
  base     = Σ round(rate × min/60) + Σ(addon.price × qty)     # slots + add-ons
  discount = percent → min(round(base × value/100), maxDiscount?)   (banker's rounding)
             flat    → min(value, base)
  discount = min(discount, base)
  amount   = max(0, base − discount) + ₹20 fee                 # fee is NOT discounted
  ```

  Server **validates** the coupon against the venue's `detail.offers`: unknown
  code / expired / below `minAmount` → `400 { "message" }`. A blank/absent code
  matches the venue's auto-apply (`code == ""`) offer. The applied `offer` and
  `discountAmount` are **persisted and echoed** on the booking (my-bookings +
  dashboard). Walk-ins apply the discount too (no ₹20 fee). Migration `0005`
  adds `Booking.offer` + `discount_amount` (nullable/default — existing rows safe).

---

## 🎉 All backlog items complete

Every item in `BACKEND_ISSUES.md` and `BACKEND_UNITS_SPEC.md` is done and live.
Each was test-first and pushed to `main` (Render auto-deploys). **Tests: 146 passing.**

Not requested but noted for later: add-on prices are trusted from the request
(re-validate when real payments land); `POST /venues/drafts/:id/submit` could
build the listing server-side (spec §6) to drop the client-side publish step.
