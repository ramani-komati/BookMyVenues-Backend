# Event ticketing API

Ticketed events (Navaratri, Dandiya, concerts…) are a **separate product from
venue bookings** and live in the `events` app with their own models, endpoints
and orders. Nothing in `bookings/` changed except one webhook fall-through.

**Why separate:** a venue booking is *exclusive* — `bookings/views.overlaps()`
guarantees one customer per slot. An event is the opposite: many customers buy
into the same date and time until the ticket capacity runs out. Teaching the
overlap check to sometimes not apply is exactly the change that breaks venue
bookings, so events never touch it.

**Who owns an event: the SUPER ADMIN, and only the super admin.** The admin
creates, edits, publishes, unpublishes and retires events from the admin
panel. The organiser is recorded on the event as plain text
(`organizerName` / `organizerPhone`) — an event needs no vendor account, no
bank details, no payout setup and no onboarding to exist. **Vendors have no
write path at all**: every vendor mutation returns `403 ADMIN_MANAGED`, and
that is enforced in the API, not by hiding a button.

Vendors keep READ-ONLY access to the events that concern them — the ones they
created before the change (which carry customers' paid tickets) and published
events hosted at one of their own venues — because they run the door on the
night.

**Customers never see a remaining-seat counter.** No browsing payload carries
`remaining`, `capacity`, `sold` or `ticketsLeft`; `soldOut`, `available` and
`bookable` say everything the UI needs. Capacity is still enforced exactly as
before — see §6.

Auth, error shape (`{"message": "…"}`), money in whole rupees, and the Razorpay
flow are all identical to the rest of the API. OTP login is unchanged —
`events` uses the same customer and vendor JWTs.

---

## 1. Public browsing (no auth)

### 1.1 List events
`GET /api/events?q=&category=&city=&locality=&page=&limit=`

`limit` ≤ 50 (default 20). Only `live` events. Cached ~60 s.

```json
{
  "events": [{
    "id", "slug", "name", "category", "description", "image", "poster",
    "location", "locality", "city", "startTime": "18:00", "endTime": "22:00",
    "dates": [{ "id", "date", "soldOut", "available", "isPast", "reason" }],
    "nextDate", "fromPrice", "soldOut", "bookable", "ended", "status",
    "ticketTypes": [{ "key", "label", "price", "seatsPerTicket", "maxPerOrder", "isActive" }]
  }],
  "total": 4,
  "categories": ["Navaratri", "Dandiya", …]
}
```

**No seat counts.** `capacity` / `remaining` / `sold` / `ticketsLeft` are not
in any customer-facing payload — the number never reaches the browser, so a
counter cannot be reintroduced by accident. Use:

- `soldOut` — every sellable night is full
- `bookable` — there is at least one night a customer could buy right now
- `ended` — the whole run is in the past (the card must not say "Book tickets")

`dates` contains only dates a customer may actually book — past dates, closed
nights and anything outside the selling window are already removed, so the
date picker can never offer something checkout would refuse.

### 1.2 Event detail
`GET /api/events/:idOrSlug` — the summary **plus** `gallery[]`, `venueName`,
`address`, `mapsLink`, `contactPhone`, `organizer`, `terms`,
`bookingOpensOn`, `bookingClosesOn`, `venueId`, and `allDates[]`.

`allDates[]` carries **every** date including past and closed ones, each with
`isPast`, so the detail page can show a finished night as "Finished" rather
than silently losing it. Past dates are *visible*, just not selectable.

Deliberately **not** cached: the payload carries live per-date bookability
(still no counts).

404 if not found or not live.

### 1.3 Availability
`GET /api/events/:id/availability?date=YYYY-MM-DD`

```json
{ "date", "soldOut": false, "available": true, "reason": "" }
```

No counts here either, for the same reason. Uncached: a stale answer about
whether a night can still be bought would be a lie.

Omit `date` to get `{ "dates": [ … ] }` — every sellable night in one call.
Never cached. 400 bad/past date · 404 unknown event or date.

---

## 2. Customer checkout (User JWT)

### 2.1 Quote
`POST /api/events/quote` `{ eventId, eventDateId|date, tickets: { adult: 2, child: 1 } }`

```json
{ "items": [{ "key","label","qty","price","seatsPerTicket","subtotal" }],
  "ticketCount": 3, "seats": 3, "subtotal": 1250, "fee": 20,
  "amount": 1270, "remaining": 188 }
```

The booking screen shows **this** breakdown. Prices come from the event's own
`TicketType` rows and the fee from `Settings.effective_fee()` — the same value
`GET /api/config` serves, so the quote and the charge cannot disagree.

### 2.2 Create order
`POST /api/events/payments/order` — same body as the quote; `amount` optional
and only **cross-checked** (mismatch → `400 AMOUNT_MISMATCH` with
`expectedAmount`), never used.

Reprices server-side, locks the date row, holds the seats as a
`payment_pending` order for 30 minutes, and creates the Razorpay order:

```json
{ "orderId", "keyId", "amount": 127000, "eventOrderId": "eo_…",
  "breakdown": { "items", "subtotal", "fee", "amount" } }
```

- **409 `SOLD_OUT`** — not enough seats; the body carries `remaining`
- 400 `DATE_UNAVAILABLE` / `NO_TICKETS` / `BAD_QUANTITY` /
  `UNKNOWN_TICKET_TYPE` / `MAX_PER_ORDER` / `MAX_TICKETS`
- 404 `DATE_NOT_FOUND` · 409 `EVENT_UNAVAILABLE` · 502 gateway unreachable

### 2.3 Verify
`POST /api/events/payments/verify`
`{ eventOrderId, razorpay_order_id, razorpay_payment_id, razorpay_signature }`

HMAC-verified, then the order is confirmed. **Idempotent** — a replay returns
the same confirmed order. A cancelled/refunded order can never be revived. If
the date sold out while the customer was paying, the order goes to
`refund_pending` (the admin Refunds panel) and answers 409 — it is never
confirmed without seats.

### 2.4 My tickets
- `GET /api/users/me/event-orders?status=upcoming|past&page=&limit=` — owner-scoped
- `DELETE /api/users/me/event-orders/:id` — releases an **unpaid hold** only
  (what the checkout calls when the widget is dismissed). A paid ticket is not
  self-service cancellable; the row is kept, never deleted, so a late capture
  can still find it and auto-refund.

### 2.5 Webhook
`POST /api/payments/webhook` — **the existing endpoint**, shared. When an order
id matches no `Booking`, `bookings.payments` falls through to
`events.payments.handle_event_webhook`. One webhook URL stays configured at
Razorpay; adding a second would have been a production dashboard change.

`payment.captured` confirms (after an amount check) · `payment.failed` cancels
and releases the seats · a capture for an already-cancelled order is
auto-refunded, never resurrected.

---

## 3. Vendor portal (Vendor JWT) — READ-ONLY

| Endpoint | Behaviour |
|---|---|
| `GET /api/vendors/me/events` | events the vendor may see, `{ events, canCreate: false, managedBy: "admin", notice }` |
| `GET /api/vendors/me/events/:id` | 404 for an event the vendor may not see |
| `GET /api/vendors/me/events/:id/orders` | who bought tickets (they run the door) |
| `POST /api/vendors/me/events` | **403** `ADMIN_MANAGED` |
| `PATCH /api/vendors/me/events/:id` | **403** `ADMIN_MANAGED` |
| `DELETE /api/vendors/me/events/:id` | **403** `ADMIN_MANAGED` |
| `POST /api/vendors/me/events/:id/poster` | **403** `ADMIN_MANAGED` |

The 403 is unconditional — it does not depend on ownership, on the request
body, or on the frontend hiding a control. The routes still exist (rather
than 404ing) so a client on an older bundle gets the reason instead of
"broken".

**What a vendor may READ**, and why:

- events they created *before* events became an admin product — those rows
  carry customers' paid tickets, and hiding them would leave the vendor
  unable to see who is turning up
- **published** events hosted at one of their own listings — someone has to
  open the gate

Every row carries `canEdit: false` and `managedBy: "admin"`, so the portal
renders the read-only state from the server's answer rather than from an
assumption that could drift from what the API allows.

---

## 4. Super admin (`/api/admin/`) — the only event write path

Cookie-session auth + `IsAdmin`, `{"detail": "…"}` error shape, **JSON only**
(admin writes are CSRF-exempt with a `SameSite=None` cookie, so requiring JSON
forces a CORS preflight and brings the origin allowlist back into play).

| Endpoint | Notes |
|---|---|
| `GET /api/admin/events` | every non-deleted event (`bootstrap` also carries `events[]`) |
| `POST /api/admin/events` | create. Saves as `draft`; pass `status: "publish"` to publish in the same call |
| `GET /api/admin/events/:id` | one event |
| `PATCH /api/admin/events/:id` | partial update: content, dates, ticket types, and/or `status` |
| `DELETE /api/admin/events/:id` | soft delete; **409 `HAS_SALES`** if tickets were sold (it is unpublished instead, and the updated row is returned) |
| `GET /api/admin/events/:id/orders?date=` | bookings + payment status |

`status` accepts `publish` / `unpublish` / `draft` (plus the legacy
`pending` / `rejected` so the old vendor-submission queue can be cleared):

- **publish** → `live` (visible and bookable)
- **unpublish** → `paused`, *not* `draft` — an event that has sold tickets
  must keep resolving for the people holding them

Posters go up through the existing `POST /api/admin/uploads` (multipart,
requires `X-Requested-With`) and arrive here as a URL.

Create / update body:

```json
{ "name", "category": "Dandiya", "description", "posterUrl",
  "venueName", "address", "locality", "city", "mapsLink",
  "organizerName": "Kakatiya Cultural Trust", "organizerPhone": "9876500001",
  "contactPhone", "terms",
  "startTime": "18:00", "endTime": "22:00",
  "bookingOpensOn": null, "bookingClosesOn": null,
  "venueId": "<any listing, optional — links the event to a listed venue>",
  "dates": [{ "date": "2026-10-20", "capacity": 500, "isAvailable": true }],
  "ticketTypes": [
    { "key": "adult", "label": "Adult", "price": 500 },
    { "key": "child", "label": "Child", "price": 250 },
    { "key": "couple", "label": "Couple", "price": 900, "seatsPerTicket": 2 }
  ] }
```

Rules enforced server-side (`events/writes.py`), 400 unless noted:

- a name, a category and a place (`venueName` or `venueId`) are required
- `adult` **and** `child` ticket types are required
- a **new** date in the past is refused; a date the event **already** runs on
  is allowed through even once it is past, so a running event can still be
  edited without deleting the nights that already happened
- capacity ≥ 1; prices ≥ 0
- `bookingOpensOn` cannot be after `bookingClosesOn`
- **409**: a date with sold tickets cannot be removed, nor its capacity cut
  below what is already sold
- a ticket type that has been sold is **deactivated**, not deleted, so
  existing orders keep rendering their line items
- publishing is refused if the event has no upcoming available date or no
  active ticket type — that would put a card on the Events page that cannot
  be bought from

Every create, edit, status change and delete writes an `AuditEntry`
server-side, with `{ reason }` recorded when supplied.

---

## 5. Config

`GET /api/config` gains `eventCategories: ["Navaratri", "Dandiya", …]`.

Kept **separate** from `categories` (the venue taxonomy) on purpose: merging
them would offer "Navaratri" as a venue type in the registration wizard and
leak it into `/api/venues` search.

---

## 6. How overselling is prevented

`events/inventory.reserve_seats()` is the only way seats are taken. It:

1. runs inside `transaction.atomic()`
2. locks the `EventDate` row with `select_for_update()`, so concurrent requests
   for the same date queue behind each other
3. **re-reads** the sold count inside the lock — never trusts the number the
   customer's browser was shown
4. the caller creates (or confirms) its order in the *same* transaction, so the
   lock is still held

Hiding the counters from customers changed **none** of this. The number was
removed from the browsing payloads only; the authority on how many seats are
left has always been this function re-reading the database under a row lock,
never anything the client was shown.

`seats_sold()` counts every order that holds seats: confirmed ones, plus
unpaid Razorpay holds younger than `PENDING_HOLD_MINUTES` (imported from
`bookings` so the two products cannot drift to different expiry rules).
Cancelled, refunded and expired holds stop counting, so an abandoned checkout
frees its seats automatically.

A `Couple` ticket consumes `seatsPerTicket` units of capacity, so selling
couples cannot oversell a venue counted in people.
