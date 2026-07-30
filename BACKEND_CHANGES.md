# Backend changes — response to `docs/BACKEND_ISSUES.md`

Living status board. Rewritten as each phase lands. Base `/api` · JWT · errors `{ "message": "..." }`.

**Last updated:** after Phase 3 · Production: `https://bookmyvenues-backend.onrender.com`

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
| 🔴 **P6** | Per-unit bookings (multi-pitch / court / screen) | ⏳ Planned (Phase 4) |

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

## Planned next

- **Phase 4:** P6 per-unit bookings — new booking fields (`sport`, `unit`,
  `unitLabel`), overlap keyed per `(venue, sport, unit)`, per-unit rate
  validation, `bookedUnits` in availability. Needs a DB migration; will inspect
  the real listing `detail` shape first.

Each phase is test-first and pushed to `main` (Render auto-deploys). Tests: 135 passing.
