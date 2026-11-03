# Clinic booking API

A small booking API for a clinic, plus a web page that uses it. Built for the
Koya Academy Cohort 4 assessment (Task 1), following the OpenAPI contract at
https://koyaacademy.com/assessment-api.

- **Stack:** Python, Flask, PostgreSQL (psycopg 3), gunicorn
- **Web page:** `static/index.html`, served by the same app at `/`
- **Levels covered:** 1 to 5

## Endpoints

| Method | Path | Level |
|---|---|---|
| POST | `/reset` | 1 |
| GET | `/slots` | 1 |
| POST | `/bookings` | 1 to 5 |
| GET | `/bookings?email=&limit=&cursor=` | 4 |
| GET | `/bookings/{id}` | 1 |
| DELETE | `/bookings/{id}` | 3 |

Every API request needs the `X-API-Key` header. The web page (`/`) and `/health` are open.
Errors always look like `{"error": "<message>"}`.

## How each level is enforced

1. **Level 1.** Two tables, `slots` and `bookings`. Slots are listed earliest first with an `available` flag.
2. **Level 2.** The key is read from the `API_KEY` environment variable and compared in constant time. Bodies are validated (missing or empty fields, bad email, unknown slot, slot already started). Names and emails are trimmed and emails are stored in lower case.
3. **Level 3.** A partial unique index on `bookings(slot_id) WHERE status = 'confirmed'` lets the database decide who wins, so two simultaneous requests cannot both book a slot. The booking is inserted with `ON CONFLICT DO NOTHING`; if nothing was inserted the API returns 409. Cancelling sets the status to `cancelled`, which frees the slot.
4. **Level 4.**
   - The `Idempotency-Key` is claimed with `INSERT ... ON CONFLICT` inside the same transaction as the booking, and the response is stored with it. A retry replays the stored status and body. The same key with a different body returns 422. Simultaneous retries wait on the first request and then replay it.
   - Bookings are listed oldest first with keyset (cursor) pagination. The cursor is opaque and `next_cursor` is `null` on the last page.
5. **Level 5.** Attempts are recorded per email (lower-cased, trimmed) in the database, under a per-email advisory lock, so the limit holds across several workers. The 6th attempt in a rolling minute gets 429 with a `Retry-After` in whole seconds. Failed attempts count; replays of an already-answered idempotent request do not.

`/reset` also clears stored idempotency keys and rate-limit counters so each test run starts clean.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (use: source .venv/bin/activate on Mac/Linux)
pip install -r requirements.txt

# set these two (PowerShell shown; use export on Mac/Linux)
$env:DATABASE_URL = "postgresql://user:password@host/dbname?sslmode=require"
$env:API_KEY = "choose-a-long-random-string"

python app.py                    # http://localhost:8000
```

Tables are created automatically on start-up and some demo slots are added if the database is empty.

## Check it

```bash
python tests/check_api.py http://localhost:8000 YOUR_API_KEY
```

The script covers all five levels, including 30 simultaneous bookings for one slot.
It calls `/reset`, so it wipes bookings and slots.

## Deploy (Neon + Render)

1. **Database:** create a project on [neon.tech](https://neon.tech) and copy the connection string.
2. **API and page:** create a Web Service on [render.com](https://render.com) from this repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 2 --threads 4 --bind 0.0.0.0:$PORT`
   - Environment variables: `DATABASE_URL` (the Neon string) and `API_KEY` (your key)
3. Open the Render URL. Paste your API key into the box at the top of the page.

Render's free plan puts the service to sleep after about 15 minutes without traffic, so the first request
can be slow. Ping `/health` every few minutes (for example with UptimeRobot) while the assessment is being reviewed.
