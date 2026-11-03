"""Clinic booking API (Koya Cohort 4, Task 1).

Stack: Flask + PostgreSQL (psycopg 3).

How each level is enforced:
  L1  slots / bookings tables, list + create + fetch endpoints.
  L2  X-API-Key header checked on every API request; body validation.
  L3  A *partial unique index* on bookings(slot_id) WHERE status = 'confirmed'
      lets the database itself guarantee one confirmed booking per slot, so
      concurrent requests cannot both win. Cancelling frees the slot.
  L4  Idempotency-Key is claimed with INSERT .. ON CONFLICT inside the same
      transaction as the booking, so retries (even simultaneous ones) replay the
      stored response. Bookings are listed with keyset (cursor) pagination.
  L5  Attempts per email are counted in the database (so it works across
      several workers) under a per-email advisory lock: 5 per rolling minute.
"""
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, make_response, request, send_from_directory
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from werkzeug.exceptions import HTTPException

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("clinic")

from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]

RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 60

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.json.sort_keys = False

# prepare_threshold=None keeps this compatible with Neon's pooled (pgbouncer)
# connection strings, which do not support server-side prepared statements.
pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=int(os.environ.get("DB_POOL_MAX", "10")),
    kwargs={"row_factory": dict_row, "prepare_threshold": None},
    check=ConnectionPool.check_connection,
    open=False,
)
pool.open()

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id        text PRIMARY KEY,
    provider  text NOT NULL,
    start_at  timestamptz NOT NULL,
    end_at    timestamptz NOT NULL,
    start_raw text NOT NULL,
    end_raw   text NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    seq        bigserial PRIMARY KEY,
    id         text NOT NULL UNIQUE,
    slot_id    text NOT NULL,
    name       text NOT NULL,
    email      text NOT NULL,
    status     text NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Level 3: at most ONE confirmed booking per slot, enforced by the database.
CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot
    ON bookings (slot_id) WHERE status = 'confirmed';

CREATE INDEX IF NOT EXISTS bookings_email_seq ON bookings (email, seq);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          text PRIMARY KEY,
    request_hash text NOT NULL,
    status_code  int,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS booking_attempts (
    email        text NOT NULL,
    attempted_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS booking_attempts_email_time
    ON booking_attempts (email, attempted_at);
"""


def utc_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def init_db():
    """Create tables and seed a few demo slots on first run."""
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(727001)")  # one worker at a time
        conn.execute(SCHEMA)
        has_slots = conn.execute("SELECT EXISTS (SELECT 1 FROM slots) AS x").fetchone()["x"]
        if not has_slots:
            now = datetime.now(timezone.utc)
            n = 0
            for day in range(1, 6):
                date = (now + timedelta(days=day)).replace(hour=0, minute=0, second=0, microsecond=0)
                for provider in ("Dr Ade", "Dr Bola"):
                    for half_hours in range(18, 24):  # 09:00 to 11:30
                        start = date + timedelta(minutes=30 * half_hours)
                        end = start + timedelta(minutes=30)
                        n += 1
                        s_raw = start.strftime("%Y-%m-%dT%H:%M:%SZ")
                        e_raw = end.strftime("%Y-%m-%dT%H:%M:%SZ")
                        conn.execute(
                            "INSERT INTO slots (id, provider, start_at, end_at, start_raw, end_raw) "
                            "VALUES (%s, %s, %s, %s, %s, %s)",
                            (f"s{n}", provider, start, end, s_raw, e_raw),
                        )


init_db()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def error(message, status, headers=None):
    resp = jsonify({"error": message})
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("not a string")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def booking_json(row):
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": utc_iso(row["created_at"]),
    }


BOOKING_COLUMNS = "id, slot_id, name, email, status, created_at"


def encode_cursor(seq):
    return base64.urlsafe_b64encode(str(seq).encode()).decode().rstrip("=")


def decode_cursor(cursor):
    try:
        seq = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
    except Exception:
        raise ValueError("bad cursor")
    if seq < 0:
        raise ValueError("bad cursor")
    return seq


def request_hash(body):
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Level 2: API key on every API request. CORS so the web page can be hosted
# anywhere. "/" (the web page) and "/health" are the only open routes.
# --------------------------------------------------------------------------
OPEN_PATHS = {"/", "/health"}


@app.before_request
def guard():
    if request.method == "OPTIONS":
        return make_response("", 204)
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    provided = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(provided.encode("utf-8"), API_KEY.encode("utf-8")):
        return error("missing or invalid API key", 401)
    return None


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key, Idempotency-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "Retry-After"
    return resp


@app.errorhandler(HTTPException)
def http_error(exc):
    return error((exc.description or exc.name).lower().rstrip("."), exc.code)


@app.errorhandler(Exception)
def unexpected(exc):
    log.exception("unhandled error")
    return error("internal server error", 500)


# --------------------------------------------------------------------------
# Open routes
# --------------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# --------------------------------------------------------------------------
# Level 1: slots
# --------------------------------------------------------------------------
@app.post("/reset")
def reset():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("slots"), list):
        return error("body must be an object with a 'slots' array", 400)

    rows, seen = [], set()
    for item in body["slots"]:
        if not isinstance(item, dict):
            return error("each slot must be an object", 400)
        sid, provider = item.get("id"), item.get("provider")
        if not isinstance(sid, str) or not sid.strip() or not isinstance(provider, str) or not provider.strip():
            return error("each slot needs a string 'id' and 'provider'", 400)
        if sid in seen:
            return error(f"duplicate slot id: {sid}", 400)
        seen.add(sid)
        try:
            start, end = parse_ts(item.get("start")), parse_ts(item.get("end"))
        except (ValueError, TypeError):
            return error("each slot needs valid 'start' and 'end' date-times", 400)
        rows.append((sid, provider, start, end, item["start"], item["end"]))

    with pool.connection() as conn:
        conn.execute("DELETE FROM bookings")
        conn.execute("DELETE FROM idempotency_keys")
        conn.execute("DELETE FROM booking_attempts")
        conn.execute("DELETE FROM slots")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO slots (id, provider, start_at, end_at, start_raw, end_raw) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                rows,
            )
    return jsonify({"ok": True, "slots": len(rows)})


@app.get("/slots")
def list_slots():
    provider = request.args.get("provider")
    with pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.provider, s.start_raw AS start, s.end_raw AS "end",
                   NOT EXISTS (
                       SELECT 1 FROM bookings b
                       WHERE b.slot_id = s.id AND b.status = 'confirmed'
                   ) AS available
            FROM slots s
            WHERE (%s::text IS NULL OR s.provider = %s)
            ORDER BY s.start_at, s.id
            """,
            (provider, provider),
        ).fetchall()
    return jsonify({"slots": rows})


# --------------------------------------------------------------------------
# Booking creation (levels 1-5)
# --------------------------------------------------------------------------
class RateLimited(Exception):
    def __init__(self, wait):
        self.wait = wait


def check_rate_limit(conn, email):
    """Level 5. Raises RateLimited if this email already made 5 attempts in the
    last minute; otherwise records this attempt.

    Every attempt that gets this far counts, whether it later succeeds or fails
    (400 / 404 / 409). The per-email advisory lock makes check-then-insert
    atomic across all workers; it is released when the transaction ends.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (email,))
    conn.execute(
        "DELETE FROM booking_attempts WHERE email = %s "
        "AND attempted_at < clock_timestamp() - make_interval(secs => %s)",
        (email, RATE_LIMIT_WINDOW_SECONDS),
    )
    rows = conn.execute(
        "SELECT attempted_at, clock_timestamp() AS now FROM booking_attempts "
        "WHERE email = %s ORDER BY attempted_at",
        (email,),
    ).fetchall()
    if len(rows) >= RATE_LIMIT_ATTEMPTS:
        # Allowed again once the 5th-most-recent attempt leaves the window.
        blocker = rows[-RATE_LIMIT_ATTEMPTS]["attempted_at"]
        remaining = (
            blocker + timedelta(seconds=RATE_LIMIT_WINDOW_SECONDS) - rows[-1]["now"]
        ).total_seconds()
        raise RateLimited(max(1, math.ceil(remaining)))
    conn.execute("INSERT INTO booking_attempts (email) VALUES (%s)", (email,))


def replay(row, req_hash):
    if row["request_hash"] != req_hash:
        return 422, {"error": "Idempotency-Key was already used with a different request body"}
    return row["status_code"], row["response"]


def create_booking(body, idem_key, req_hash, limit_email):
    """Runs in ONE transaction: claim the idempotency key, rate-limit, book,
    store the response. If we are rate limited the transaction rolls back, which
    also releases the idempotency key so a later retry can still succeed."""
    with pool.connection() as conn:
        if idem_key:
            claimed = conn.execute(
                "INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s) "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                (idem_key, req_hash),
            ).fetchone()
            if not claimed:
                # Another request holds / held this key. The INSERT above waits
                # for that transaction to finish, so its response is stored now.
                row = conn.execute(
                    "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = %s",
                    (idem_key,),
                ).fetchone()
                return replay(row, req_hash)  # a replay is not a new attempt

        if limit_email:
            check_rate_limit(conn, limit_email)

        status, payload = _book(conn, body)

        if idem_key:
            conn.execute(
                "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE key = %s",
                (status, Jsonb(payload), idem_key),
            )
        return status, payload


def _book(conn, body):
    # ---- Level 2: validation -------------------------------------------
    slot_id, name, email = body.get("slot_id"), body.get("name"), body.get("email")
    if not isinstance(slot_id, str) or not slot_id.strip():
        return 400, {"error": "slot_id is required"}
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required"}
    if not isinstance(email, str) or not email.strip():
        return 400, {"error": "email is required"}
    name, email = name.strip(), email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}

    slot = conn.execute(
        "SELECT id, start_at <= now() AS started FROM slots WHERE id = %s", (slot_id,)
    ).fetchone()
    if not slot:
        return 404, {"error": "slot not found"}
    if slot["started"]:
        return 400, {"error": "slot has already started"}

    # ---- Level 3: the unique partial index decides who wins -------------
    row = conn.execute(
        f"""
        INSERT INTO bookings (id, slot_id, name, email, status)
        VALUES (%s, %s, %s, %s, 'confirmed')
        ON CONFLICT (slot_id) WHERE status = 'confirmed' DO NOTHING
        RETURNING {BOOKING_COLUMNS}
        """,
        ("b_" + secrets.token_hex(8), slot_id, name, email),
    ).fetchone()
    if row is None:
        return 409, {"error": "slot already booked"}
    return 201, booking_json(row)


@app.post("/bookings")
def post_booking():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return error("body must be a JSON object", 400)

    idem_key = (request.headers.get("Idempotency-Key") or "").strip() or None
    req_hash = request_hash(body)

    # Level 4: a retry of something we already answered is replayed untouched
    # (and is not counted as a new attempt).
    if idem_key:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = %s",
                (idem_key,),
            ).fetchone()
        if row and row["status_code"] is not None:
            status, payload = replay(row, req_hash)
            return make_response(jsonify(payload), status)

    # Level 5 is applied inside create_booking (after the idempotency key is
    # claimed), so retries that only replay a stored response never count.
    raw_email = body.get("email")
    limit_email = raw_email.strip().lower() if isinstance(raw_email, str) and raw_email.strip() else None

    try:
        status, payload = create_booking(body, idem_key, req_hash, limit_email)
    except RateLimited as limited:
        return error("too many booking attempts, slow down", 429, {"Retry-After": str(limited.wait)})
    return make_response(jsonify(payload), status)


# --------------------------------------------------------------------------
# Level 4: list a person's bookings (cursor pagination)
# --------------------------------------------------------------------------
@app.get("/bookings")
def list_bookings():
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return error("email query parameter is required", 400)

    raw_limit = request.args.get("limit")
    if raw_limit is None:
        limit = 10
    else:
        try:
            limit = int(raw_limit.strip())
        except ValueError:
            return error("limit must be an integer between 1 and 50", 400)
        if not 1 <= limit <= 50:
            return error("limit must be an integer between 1 and 50", 400)

    after = 0
    cursor = request.args.get("cursor")
    if cursor:
        try:
            after = decode_cursor(cursor)
        except ValueError:
            return error("invalid cursor", 400)

    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT seq, {BOOKING_COLUMNS} FROM bookings "
            "WHERE email = %s AND seq > %s ORDER BY seq LIMIT %s",
            (email, after, limit + 1),
        ).fetchall()

    has_more = len(rows) > limit
    page = rows[:limit]
    return jsonify(
        {
            "bookings": [booking_json(r) for r in page],
            "next_cursor": encode_cursor(page[-1]["seq"]) if has_more else None,
        }
    )


# --------------------------------------------------------------------------
# Level 1 / 3: get and cancel one booking
# --------------------------------------------------------------------------
@app.get("/bookings/<booking_id>")
def get_booking(booking_id):
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT {BOOKING_COLUMNS} FROM bookings WHERE id = %s", (booking_id,)
        ).fetchone()
    if not row:
        return error("booking not found", 404)
    return jsonify(booking_json(row))


@app.delete("/bookings/<booking_id>")
def cancel_booking(booking_id):
    with pool.connection() as conn:
        row = conn.execute(
            f"UPDATE bookings SET status = 'cancelled' "
            f"WHERE id = %s AND status = 'confirmed' RETURNING {BOOKING_COLUMNS}",
            (booking_id,),
        ).fetchone()
        if row:
            return jsonify(booking_json(row))
        exists = conn.execute("SELECT 1 FROM bookings WHERE id = %s", (booking_id,)).fetchone()
    if not exists:
        return error("booking not found", 404)
    return error("booking is already cancelled", 409)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
