# Book My Venue — Backend API

Django + DRF backend for vendor venue registration. All endpoints live under `/api/v1/`.

- **Auth:** JWT Bearer tokens (access valid 1 day, refresh 30 days)
- **Base URL (local):** `http://127.0.0.1:8000`
- **Content type:** `application/json` for all request bodies

## Local setup

```bash
python -m venv env && source env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit values (SECRET_KEY at minimum)
python manage.py migrate
python manage.py runserver
```

## Environment variables (.env)

| Variable | Meaning |
|---|---|
| `SECRET_KEY` | Django crypto key — required, app refuses to start without it |
| `DEBUG` | `True` locally, `False` in production |
| `ALLOWED_HOSTS` | Comma-separated hostnames, e.g. `localhost,myapp.onrender.com` |
| `DATABASE_URL` | Postgres connection string (Supabase). Empty = local sqlite |
| `CORS_ALLOW_ALL_ORIGINS` | `True` in dev only |
| `CORS_ALLOWED_ORIGINS` | Comma-separated frontend URLs (used in production) |

## Error shape (consistent everywhere)

- Validation errors → `400` with field-level messages: `{"phone": ["Phone number must be exactly 10 digits."]}`
- Submit gaps → `400` `{"missing": ["Venue name", ...]}`
- Auth/permission/not-found → `{"detail": "..."}` with `401` / `403` / `404`
- List endpoints are paginated: `{"count", "next", "previous", "results"}` (`?page=2&page_size=50`, max 100)

---

## Health

```bash
curl http://127.0.0.1:8000/api/v1/health
# {"status":"ok"}
```

## Auth

### Register a vendor

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/vendor/register \
  -H "Content-Type: application/json" \
  -d '{"name":"Ravi Kumar","phone":"9876543210","email":"ravi@example.com","password":"secret123"}'
# 201 {"token":"<access>","refresh":"<refresh>","user":{"id":1,"name":"Ravi Kumar","phone":"9876543210","role":"VENDOR"}}
```

Validation: phone exactly 10 digits & unique, email valid & unique, password ≥ 8 chars.

### Login

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"phone":"9876543210","password":"secret123"}'
# 200 same shape as register | 401 {"detail":"Invalid phone or password."}
```

Login/register are rate-limited (HTTP 429 after ~10 requests/min per IP).

### Current user

```bash
curl http://127.0.0.1:8000/api/v1/auth/me -H "Authorization: Bearer $TOKEN"
# 200 {"id":1,"name":"Ravi Kumar","phone":"9876543210","role":"VENDOR"}
```

### Refresh an expired access token

```bash
curl -X POST http://127.0.0.1:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" -d '{"refresh":"<refresh-token>"}'
# 200 {"access":"<new-access-token>"}
```

---

## Venues (vendor role required — all IDs are scoped to the logged-in vendor; someone else's venue id returns 404)

### Create an empty draft

```bash
curl -X POST http://127.0.0.1:8000/api/v1/vendor/venues -H "Authorization: Bearer $TOKEN"
# 201 {"id":1,"status":"DRAFT","completion":0}
```

### List my venues

```bash
curl http://127.0.0.1:8000/api/v1/vendor/venues -H "Authorization: Bearer $TOKEN"
# 200 {"count":1,"next":null,"previous":null,"results":[{"id":1,"name":"","category":null,"status":"DRAFT","completion":0}]}
```

### Get one venue (full object)

```bash
curl http://127.0.0.1:8000/api/v1/vendor/venues/1 -H "Authorization: Bearer $TOKEN"
```

Returns every field plus `units`, `packages`, `sports`, `addons`, `photos`,
`completion` (0–100, steps of 20) and `missing` (human-readable list of gaps —
drive the wizard progress circle from these two).

### Save wizard progress (partial update)

Allowed only in `DRAFT` or `REJECTED` status (editing a REJECTED venue moves it
back to DRAFT and clears `rejection_reason`). Send **any subset** of fields:

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/vendor/venues/1 \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "name": "Grand Palace Hall",
    "contact_phone": "9876543210",
    "address_line": "12 MG Road",
    "pincode": "560001",
    "state": "Karnataka",
    "maps_link": "https://maps.google.com/?q=grand+palace",
    "category": "PRIVATE_HALL",
    "amenities": ["WiFi", "AC"],
    "occasions": ["Wedding", "Birthday"],
    "units": [{"label": "Hall 1", "max_persons": 200}]
  }'
# 200 {"id":1,"completion":80,"missing":["At least one venue photo","Payout details"],"saved_at":"..."}
```

Nested lists (`units`, `packages`, `sports`, `addons`) **replace** all existing
rows of that kind when provided; omit a list to leave it untouched.

Field reference for nested rows:

```jsonc
"units":    [{"label": "Hall 1", "max_persons": 200}],
"packages": [{"label": "Gold", "details": "Decor + DJ", "price": "4999.00",
              "duration_hrs": 3, "max_persons": 50, "charge_per_hour": false}],
"sports":   [{"sport": "BOX_CRICKET", "price_per_hour": "1200.00",
              "capacity_type": "LIMITED", "max_persons": 12, "pitches": 2}],
"addons":   [{"name": "Photographer", "price": "2000.00", "quantity_based": false}]
```

Category rules (400 with a field error if violated):

| Rule | Applies to |
|---|---|
| `occasions` only for `PRIVATE_HALL` | others rejected |
| `sports` / `site_capacity` only for `PLAYZONE` | others rejected |
| `extra_person_price` / `extra_person_max` NOT allowed for `RESORT` | rejected |
| `capacity_type: "LIMITED"` requires `max_persons` | sports rows |

Categories: `PRIVATE_HALL`, `PRIVATE_THEATRE`, `OPEN_THEATRE`, `RESORT`, `PLAYZONE`.
Sports: `BOX_CRICKET`, `BADMINTON`, `VOLLEYBALL`, `BASKETBALL`, `SWIMMING_POOL`, `PICKLEBALL`, `FOOTBALL`.

### Photos (URLs only — upload the file to Cloudinary first)

```bash
# Add (max 5 per type; types: VENUE, SERVICE)
curl -X POST http://127.0.0.1:8000/api/v1/vendor/venues/1/photos \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"image_url":"https://res.cloudinary.com/demo/hall.jpg","type":"VENUE"}'
# 201 {"id":7,"image_url":"...","type":"VENUE","order":0}
# 400 {"detail":"Maximum 5 photos allowed for type VENUE."} when full

# Delete
curl -X DELETE http://127.0.0.1:8000/api/v1/vendor/venues/1/photos/7 \
  -H "Authorization: Bearer $TOKEN"
# 204
```

### Delete a venue (soft delete)

```bash
curl -X DELETE http://127.0.0.1:8000/api/v1/vendor/venues/1 -H "Authorization: Bearer $TOKEN"
# 204 — venue disappears from all lists/lookups
```

### Payout details (one set per vendor, shared across their venues)

```bash
# Read (all fields null until saved; account_number always masked)
curl http://127.0.0.1:8000/api/v1/vendor/payout -H "Authorization: Bearer $TOKEN"

# Save / overwrite
curl -X PUT http://127.0.0.1:8000/api/v1/vendor/payout \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "account_holder": "Ravi Kumar",
    "bank_name": "HDFC Bank",
    "account_number": "123456789012",
    "ifsc": "HDFC0001234",
    "payout_phone": "9876543210",
    "upi_id": "ravi@upi",
    "pan": "ABCDE1234F"
  }'
# 200 {..., "account_number": "********9012", ...}
```

Validation: account number 9–18 digits, IFSC `AAAA0XXXXXX`, phone 10 digits,
`upi_id`/`pan` optional (PAN validated when given).

### Submit for review

```bash
curl -X POST http://127.0.0.1:8000/api/v1/vendor/venues/1/submit -H "Authorization: Bearer $TOKEN"
# 200 {"id":1,"status":"PENDING","detail":"Submitted for review."}
# 200 {"detail":"Already submitted."}            (idempotent)
# 400 {"missing":["Venue name","At least one venue photo", ...]}  (incomplete)
```

Submit requires: name, contact phone, address line, 6-digit pincode, state, maps
link, category, ≥1 amenity, ≥1 VENUE photo, payout details saved, every unit has
`max_persons`, and for PLAYZONE ≥1 priced sport.

Status flow: `DRAFT → PENDING → LIVE | REJECTED` (admin decides; a REJECTED venue
returns to DRAFT when the vendor edits it).

---

## Deploying on Render

1. **Web Service** → connect this repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `gunicorn BookMyVenue.wsgi:application`
4. Pre-deploy command (or run once in Shell): `python manage.py migrate`
5. Environment variables: set everything from `.env.example` —
   `DEBUG=False`, `ALLOWED_HOSTS=<your-app>.onrender.com`,
   `DATABASE_URL=<Supabase pooler connection string, port 6543>`,
   `CORS_ALLOW_ALL_ORIGINS=False`, `CORS_ALLOWED_ORIGINS=<frontend URL>`.
