"""
Live smoke test for the pending-clarification fast path (llm_location.py /
appointments_db.py) through the ACTUAL /location HTTP endpoint - now that
app.py's FlexLocationRequest forwards `phone` through.

What it checks: once a clarification like "Dahisar East or Dahisar West?"
has been asked, the next reply must resolve LOCALLY (no second LLM call) for
an exact match, a bare direction word, and a common misspelling - and must
still degrade gracefully (no crash, pending state kept) for a reply that
matches neither.

It seeds the pending-clarification state directly via appointments_db rather
than relying on the live LLM to mark something ambiguous on a given run -
the model's ambiguous/candidate_localities output isn't fully deterministic
turn to turn, and that's not what this script is testing anyway. What's
being tested is: GIVEN a pending clarification, does the next reply resolve
correctly. That's the part that changed.

Requires the server already running (separate terminal):
    uvicorn app:app --host 0.0.0.0 --port 8000

Run (from the project folder, so it shares appointments.db with the server):
    python test_pending_clarification_live.py [base_url]
    (base_url defaults to http://localhost:8000)
"""

import sys
import time

import requests

import appointments_db

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

results = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and status == "FAIL" else ""))
    return condition


def call_location(message, phone):
    resp = requests.post(f"{BASE_URL}/location", json={"message": message, "phone": phone}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fresh_phone():
    return "9199990" + str(int(time.time() * 1000) % 100000)


def seed_dahisar_clarification(phone):
    """Puts `phone` into the exact state _resolve() leaves it in right after
    asking 'Dahisar East or Dahisar West?' - no LLM call needed to set this up."""
    appointments_db.save_pending_clarification(phone, ["Dahisar East", "Dahisar West"])
    appointments_db.reset_location_retry(phone)


def cleanup(phone):
    appointments_db.clear_pending_clarification(phone)
    appointments_db.reset_location_retry(phone)


print(f"Testing against {BASE_URL}\n")

try:
    h = requests.get(f"{BASE_URL}/health", timeout=10).json()
    check("server is up", True)
    check("groq key loaded", "groq" in h.get("llm", "").lower(), h.get("llm"))
except Exception as e:
    check("server is up", False, str(e))
    print("\nServer not reachable - start it first: uvicorn app:app --host 0.0.0.0 --port 8000")
    sys.exit(1)

# 1. exact (case-insensitive) match against a candidate
phone = fresh_phone()
seed_dahisar_clarification(phone)
out = call_location("Dahisar West", phone)
check("exact match -> Dahisar West", out.get("normalized_location") == "Dahisar West", out)
check("exact match clears pending state", appointments_db.get_pending_clarification(phone) == [], out)
cleanup(phone)

# 2. bare direction word
phone = fresh_phone()
seed_dahisar_clarification(phone)
out = call_location("west", phone)
check('bare "west" -> Dahisar West (resolved locally)', out.get("normalized_location") == "Dahisar West", out)
cleanup(phone)

phone = fresh_phone()
seed_dahisar_clarification(phone)
out = call_location("East", phone)
check('bare "East" (mixed case) -> Dahisar East', out.get("normalized_location") == "Dahisar East", out)
cleanup(phone)

# 3. misspelled direction
phone = fresh_phone()
seed_dahisar_clarification(phone)
out = call_location("esat", phone)
check('misspelled "esat" -> Dahisar East', out.get("normalized_location") == "Dahisar East", out)
cleanup(phone)

# 4. reply that matches neither -> falls through to the LLM, doesn't crash,
#    and keeps the pending candidates around for a second guess
phone = fresh_phone()
seed_dahisar_clarification(phone)
out = call_location("banana", phone)
check("unmatched reply does not 500 / crash", out.get("needs_clarification") in ("yes", "no"), out)
still_pending = appointments_db.get_pending_clarification(phone)
check("pending state kept after a bad guess", still_pending == ["Dahisar East", "Dahisar West"], still_pending)
out2 = call_location("west", phone)
check('second guess "west" after a bad first guess -> Dahisar West',
      out2.get("normalized_location") == "Dahisar West", out2)
cleanup(phone)

# 5. sanity check: nothing pending -> must NOT fabricate a match
phone = fresh_phone()
cleanup(phone)
out = call_location("west", phone)
check("no pending state -> does not fabricate Dahisar West", out.get("normalized_location") != "Dahisar West", out)
cleanup(phone)

print()
failures = [r for r in results if r[0] == "FAIL"]
print(f"{len(results) - len(failures)}/{len(results)} checks passed")
if failures:
    print("\nFAILURES:")
    for status, name, detail in failures:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print("All checks passed - safe to deploy.")
