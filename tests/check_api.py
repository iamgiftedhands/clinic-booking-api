"""End-to-end checks for all five levels.

Usage:
    python tests/check_api.py https://your-api.onrender.com YOUR_API_KEY

WARNING: this calls POST /reset, which wipes all bookings and slots.
Run it before you submit, then call /reset once more (or restart) so the
page has demo slots again.
"""
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

BASE = sys.argv[1].rstrip("/")
KEY = sys.argv[2]
H = {"X-API-Key": KEY}
passed = failed = 0


def check(label, ok, extra=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {label}")
    else:
        failed += 1
        print(f"  FAIL {label} {extra}")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def fresh_email():
    return f"{uuid.uuid4().hex[:10]}@example.com"


now = datetime.now(timezone.utc)
future = (now + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
slots = []
for i in range(40):
    start = future + timedelta(minutes=30 * i)
    slots.append(
        {
            "id": f"t{i}",
            "provider": "Dr Ade" if i % 2 == 0 else "Dr Bola",
            "start": iso(start),
            "end": iso(start + timedelta(minutes=30)),
        }
    )
past_start = now - timedelta(hours=2)
slots.append({"id": "past", "provider": "Dr Ade", "start": iso(past_start), "end": iso(past_start + timedelta(minutes=30))})


def post_booking(slot_id, email=None, name="Ada Obi", headers=None):
    h = dict(H)
    h.update(headers or {})
    return requests.post(
        f"{BASE}/bookings",
        json={"slot_id": slot_id, "name": name, "email": email or fresh_email()},
        headers=h,
        timeout=30,
    )


print("Level 2: API key")
r = requests.get(f"{BASE}/slots", timeout=30)
check("no key -> 401", r.status_code == 401 and "error" in r.json())
r = requests.get(f"{BASE}/slots", headers={"X-API-Key": "wrong"}, timeout=30)
check("wrong key -> 401", r.status_code == 401)
r = requests.post(f"{BASE}/reset", json={"slots": []}, timeout=30)
check("reset without key -> 401", r.status_code == 401)

print("Level 1: slots")
r = requests.post(f"{BASE}/reset", json={"slots": slots}, headers=H, timeout=30)
check("reset -> 200", r.status_code == 200, r.text)
r = requests.get(f"{BASE}/slots", headers=H, timeout=30)
data = r.json()["slots"]
check("lists all slots", len(data) == len(slots))
check("earliest start first", [s["start"] for s in data] == sorted(s["start"] for s in data))
check("all available", all(s["available"] for s in data))
check("slot shape", set(data[0]) == {"id", "provider", "start", "end", "available"})
r = requests.get(f"{BASE}/slots", params={"provider": "Dr Bola"}, headers=H, timeout=30)
check("provider filter", r.status_code == 200 and r.json()["slots"] and all(s["provider"] == "Dr Bola" for s in r.json()["slots"]))

print("Level 1 + 2: create / get / validation")
email = "  Ada@Example.COM "
r = requests.post(f"{BASE}/bookings", json={"slot_id": "t0", "name": "  Ada Obi  ", "email": email}, headers=H, timeout=30)
b = r.json()
check("create -> 201", r.status_code == 201, r.text)
check("trims name, lowercases email", b.get("name") == "Ada Obi" and b.get("email") == "ada@example.com", str(b))
check("status confirmed", b.get("status") == "confirmed")
check("booking shape", set(b) == {"id", "slot_id", "name", "email", "status", "created_at"})
r = requests.get(f"{BASE}/bookings/{b['id']}", headers=H, timeout=30)
check("get booking", r.status_code == 200 and r.json() == b)
r = requests.get(f"{BASE}/bookings/nope", headers=H, timeout=30)
check("unknown booking -> 404", r.status_code == 404 and "error" in r.json())
slot0 = [s for s in requests.get(f"{BASE}/slots", headers=H, timeout=30).json()["slots"] if s["id"] == "t0"][0]
check("booked slot unavailable", slot0["available"] is False)

bad_bodies = [
    {"name": "A", "email": "a@b.co"},
    {"slot_id": "t1", "email": "a@b.co"},
    {"slot_id": "t1", "name": "A"},
    {"slot_id": "", "name": "A", "email": "a@b.co"},
    {"slot_id": "t1", "name": "   ", "email": "a@b.co"},
    {"slot_id": "t1", "name": "A", "email": ""},
    {"slot_id": "t1", "name": "A", "email": "not-an-email"},
    {"slot_id": "t1", "name": "A", "email": "a@b"},
    {"slot_id": "t1", "name": 5, "email": "a@b.co"},
]
codes = [requests.post(f"{BASE}/bookings", json=x, headers=H, timeout=30).status_code for x in bad_bodies]
check("missing/invalid fields -> 400", codes == [400] * len(bad_bodies), str(codes))
r = requests.post(f"{BASE}/bookings", data="not json", headers={**H, "Content-Type": "application/json"}, timeout=30)
check("malformed JSON -> 400", r.status_code == 400)
r = post_booking("does-not-exist")
check("unknown slot -> 404", r.status_code == 404)
r = post_booking("past")
check("slot already started -> 400", r.status_code == 400)

print("Level 3: no double booking")
r = post_booking("t0")
check("already booked -> 409", r.status_code == 409 and "error" in r.json())
with ThreadPoolExecutor(max_workers=30) as pool:
    results = list(pool.map(lambda _: post_booking("t1").status_code, range(30)))
check("30 simultaneous -> exactly one 201", results.count(201) == 1 and results.count(409) == 29, str(sorted(set(results))))
r = requests.delete(f"{BASE}/bookings/{b['id']}", headers=H, timeout=30)
check("cancel -> 200 cancelled", r.status_code == 200 and r.json()["status"] == "cancelled")
check("cancel again -> 409", requests.delete(f"{BASE}/bookings/{b['id']}", headers=H, timeout=30).status_code == 409)
check("cancel unknown -> 404", requests.delete(f"{BASE}/bookings/nope", headers=H, timeout=30).status_code == 404)
check("slot free after cancel", requests.post(f"{BASE}/bookings", json={"slot_id": "t0", "name": "B", "email": fresh_email()}, headers=H, timeout=30).status_code == 201)
check("get shows cancelled", requests.get(f"{BASE}/bookings/{b['id']}", headers=H, timeout=30).json()["status"] == "cancelled")

print("Level 4: idempotency")
key = uuid.uuid4().hex
em = fresh_email()
r1 = post_booking("t2", email=em, headers={"Idempotency-Key": key})
r2 = post_booking("t2", email=em, headers={"Idempotency-Key": key})
check("first -> 201", r1.status_code == 201)
check("retry -> same status and id", r2.status_code == 201 and r2.json()["id"] == r1.json()["id"], r2.text)
r3 = requests.post(f"{BASE}/bookings", json={"slot_id": "t3", "name": "Ada Obi", "email": em}, headers={**H, "Idempotency-Key": key}, timeout=30)
check("same key, different body -> 422", r3.status_code == 422 and "error" in r3.json(), r3.text)
check("no second booking created", requests.get(f"{BASE}/bookings", params={"email": em}, headers=H, timeout=30).json()["bookings"].__len__() == 1)
key2, em2 = uuid.uuid4().hex, fresh_email()
with ThreadPoolExecutor(max_workers=10) as pool:
    rs = list(pool.map(lambda _: post_booking("t4", email=em2, headers={"Idempotency-Key": key2}), range(10)))
ids = {r.json().get("id") for r in rs}
check("10 simultaneous retries -> one booking, same response", len(ids) == 1 and all(r.status_code == 201 for r in rs), str([r.status_code for r in rs]))
key3 = uuid.uuid4().hex
em3 = fresh_email()
a = post_booking("t0", email=em3, headers={"Idempotency-Key": key3})
c = post_booking("t0", email=em3, headers={"Idempotency-Key": key3})
check("replayed 409 stays 409", a.status_code == 409 and c.status_code == 409)

print("Level 4: pagination")
# The rate limit allows 5 attempts per email per minute, so use 5 bookings.
pe = fresh_email()
for i in range(5, 10):
    post_booking(f"t{i}", email=pe, name=f"P{i}")
    time.sleep(0.02)
r = requests.get(f"{BASE}/bookings", params={"email": pe.upper(), "limit": 2}, headers=H, timeout=30)
p1 = r.json()
check("page 1 has 2 + cursor", r.status_code == 200 and len(p1["bookings"]) == 2 and p1["next_cursor"])
seen, cursor, pages = [], None, 0
while True:
    params = {"email": pe, "limit": 2}
    if cursor:
        params["cursor"] = cursor
    page = requests.get(f"{BASE}/bookings", params=params, headers=H, timeout=30).json()
    seen += [x["name"] for x in page["bookings"]]
    pages += 1
    cursor = page["next_cursor"]
    if not cursor:
        break
check("walks every booking oldest first, no repeats", seen == [f"P{i}" for i in range(5, 10)] and pages == 3, str(seen))
exact = requests.get(f"{BASE}/bookings", params={"email": pe, "limit": 5}, headers=H, timeout=30).json()
check("exact last page has null cursor", len(exact["bookings"]) == 5 and exact["next_cursor"] is None)
check("default limit works", len(requests.get(f"{BASE}/bookings", params={"email": pe}, headers=H, timeout=30).json()["bookings"]) == 5)
check("bad cursor -> 400", requests.get(f"{BASE}/bookings", params={"email": pe, "cursor": "!!!"}, headers=H, timeout=30).status_code == 400)
for bad in ("0", "51", "abc", "-1", ""):
    code = requests.get(f"{BASE}/bookings", params={"email": pe, "limit": bad}, headers=H, timeout=30).status_code
    check(f"limit={bad!r} -> 400", code == 400, str(code))
check("unknown email -> empty list", requests.get(f"{BASE}/bookings", params={"email": fresh_email()}, headers=H, timeout=30).json() == {"bookings": [], "next_cursor": None})

print("Level 5: rate limit")
spam = fresh_email()
codes = [post_booking("t20", email=spam).status_code for _ in range(7)]
check("first 5 attempts get through to the booking logic", codes[:5] == [201, 409, 409, 409, 409], str(codes))
check("6th and 7th -> 429", codes[5:] == [429, 429], str(codes))
r = post_booking("t20", email=spam)
ra = r.headers.get("Retry-After", "")
check("Retry-After is whole seconds (1-60)", r.status_code == 429 and ra.isdigit() and 1 <= int(ra) <= 60, ra)
check("other emails unaffected", post_booking("t21").status_code == 201)
check("case/space variants share the limit", post_booking("t22", email="  " + spam.upper() + " ").status_code == 429)
em_burst = fresh_email()
with ThreadPoolExecutor(max_workers=20) as pool:
    burst = list(pool.map(lambda i: post_booking(f"t{23 + i % 10}", email=em_burst).status_code, range(20)))
check("20 simultaneous attempts -> exactly 5 allowed", sum(1 for c in burst if c != 429) == 5, str(sorted(burst)))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
