# Backend changes — response to `docs/BACKEND_ISSUES.md`

Living status board. Rewritten as each phase lands. Base `/api` · JWT · errors `{ "message": "..." }`.

**Last updated:** after Phase 1 · Production: `https://bookmyvenues-backend.onrender.com`

---

## Status at a glance

| # | Item | Status |
|---|------|--------|
| 🔴 **P1** | Booking accepts packages / extra-persons / custom add-ons | ✅ **DONE** (shipped) |
| 🟠 **P2** | `GET /maps/resolve` — resolve Google Maps short links | ⏳ Planned (Phase 3) |
| 🟠 **P3** | `gallery` in venue list + dashboard `venues[]` | ⏳ Planned (Phase 2) |
| 🟡 **P4** | `PATCH /users/me` + `PATCH /vendors/me` (profile edits) | ⏳ Planned (Phase 2) |
| 🟡 **P5a** | Structured amount-mismatch error (`code`, `expectedAmount`) | ⏳ Planned (Phase 2) |
| 🟡 **P5b** | JSON `{ "message" }` 404s for bad/non-UUID ids | ⏳ Planned (Phase 2) |
| ✅ **P5c** | Pagination for `GET /venues` | ✅ **Already works** — see note below |
| 🔴 **P6** | Per-unit bookings (multi-pitch / court / screen) | ⏳ Planned (Phase 4) |

---

## ✅ P1 — Bookings now accept packages, extra-persons & custom add-ons

**Shipped.** The booking endpoint no longer rejects add-on names it doesn't
recognise, and it prices the add-on lines **from the request**.

`POST /users/me/bookings` recomputes:

```
amount = round(hourlyRate × minutes / 60)     ← slot: from the listing (server-authoritative)
       + Σ(addon.price × addon.qty)           ← add-ons: taken from the REQUEST
       + ₹20 fee
```

- Packages and extra-persons folded into `addons` as line items are accepted,
  priced, **persisted, and echoed** in the booking record (2.3 my-bookings,
  3.4 dashboard `allBookings`) — no frontend change needed.
- Custom add-ons (e.g. `smoke`, `juice`) no longer 400 with `Unknown add-on`.
- Guards kept: each add-on needs a name, `qty ≥ 1`, `price ≥ 0`. The slot
  portion is still computed from the listing (not trusted from the client),
  and the total is still checked against the client's `amount` (mismatch → 400).

> **Security note:** add-on prices are currently **trusted from the request**.
> Fine today because no live payment moves money (amount is a recorded number).
> When real payments land we'll re-validate add-on prices against the listing
> catalogue + `detail.packages[]` + `detail.extraPersonPrice`.

Walk-ins (`POST /vendors/me/walkin-bookings`) unchanged — the walk-in modal
sends no add-ons; amount stays `round(perSlot × minutes / 60)`, no fee.

---

## ✅ P5c — Pagination already supported

`GET /venues?page=2&limit=20` already works today (offset paging, `limit` 1–50,
default 20; response includes `total`). If venues past the first page aren't
showing, send `?page=`. No backend change required.

---

## Planned next (in order)

- **Phase 2 (quick wins):** P3 `gallery` in list responses · P4 profile
  endpoints · P5a structured amount error · P5b JSON 404s.
- **Phase 3:** P2 `GET /maps/resolve` (with an SSRF host allowlist:
  `maps.app.goo.gl`, `goo.gl`, `g.co` only).
- **Phase 4:** P6 per-unit bookings (needs a DB migration; will inspect the
  real listing `detail` shape first).

Each phase is test-first and pushed to `main` (Render auto-deploys).
