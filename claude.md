# Indihomes WhatsApp Assistant — Project Context

## What this project is

A WhatsApp property assistant for Indihomes (Mumbai real estate), built on WATI
(WhatsApp Business API platform) with a FastAPI backend. WATI runs the
conversation; this backend does all the intelligence: location understanding,
property search, and (new) appointment booking.

**Working directory:** `C:\Users\admin\Desktop\wati-webhook`
**Run:** `uvicorn app:app --host 0.0.0.0 --port 8000`
**Expose:** `ngrok http 8000` (WATI must reach the backend over the public internet)
**Current public URL:** `https://jawed-oven-climate.ngrok-free.dev` (free ngrok; changes on restart)

## Existing files (DO NOT rewrite these unless asked)

| File | Purpose |
|---|---|
| `app.py` | FastAPI entrypoint. All routes live here. Loads `.env` first. |
| `property_core.py` | Loads `properties.json`, normalizes it, exposes `search()` and `KNOWN_LOCALITIES`. Single source of truth for search. |
| `llm_location.py` | Groq-powered location extraction + ambiguity detection. |
| `properties.json` | 74 property records (will be replaced with live Indihomes data later). |
| `.env` | `GROQ_API_KEY=...` — never commit. |

### Existing endpoints
- `POST /search` — main endpoint. Location understanding + property search in one call. Returns `recommendations`, `count`, `name1..3`, `detail1..3`, `image1..3`.
- `POST /location` — location extraction only. Returns `needs_clarification` ("yes"/"no"), `clarify_question`, `normalized_location`.
- `POST /api/property-search` — search only, expects a clean location.
- `GET /health` — sanity check: property count, localities, LLM key status.

## CRITICAL: WATI's two variable syntaxes

This cost days of debugging. Get it right:

- **Inside webhook request bodies → `@variable`**
  e.g. `{"location": "@location", "budget": "@budget"}`
- **Inside message/question text → `{{variable}}`**
  e.g. `Thanks! {{recommendations}}`

Using `{{ }}` in a webhook body sends the literal characters `{{location}}` to the
backend. The backend defensively strips any value matching `{{...}}` to empty —
keep that behaviour in any new endpoint (see `_clean_incoming` in `app.py`).

Other WATI constraints learned the hard way:
- Interactive buttons are **static**. A webhook CANNOT render dynamic buttons.
  Any list of choices must be sent as **text** and the user replies with a number.
- Response variables must be saved as **Custom Attribute** with the plain field
  name as the path (e.g. `recommendations`), and the webhook must be tested once
  in the builder before the mapping binds.
- Button `nodeResultId` overrides graph edges. If a button should go somewhere
  new, change `nodeResultId`, not just the edge.

---

# TASK: Add appointment booking via Google Calendar

## Goal

When a user picks **"Book a Site Visit"** or **"Talk to an Advisor"**, show them
real free slots from a shared Google Calendar, let them pick one by replying with
a number, create the calendar event, email the advisor, and store the appointment.

## Design decisions already made (do not redesign)

1. **Slot picker = numbered text list.** WATI cannot render dynamic buttons.
   The backend returns a formatted string; the user replies "2".
2. **One shared Google Calendar**, service-account owned. The 3 advisors are added
   as **event attendees** by email, so Google emails them the invite automatically.
   This works whether their emails are Workspace or personal Gmail.
3. **SQLite** (`appointments.db`) in the project folder. No external DB.
4. **Round-robin advisor assignment** across the 3 advisors.

## What to build

### 1. `calendar_service.py` (new)

Google Calendar integration. Uses a service account.

```python
# Required env vars (add to .env):
#   GOOGLE_CALENDAR_ID=<shared calendar id>
#   GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
#   ADVISOR_EMAILS=advisor1@x.com,advisor2@x.com,advisor3@x.com
#   BUSINESS_HOURS_START=10      # 10 AM
#   BUSINESS_HOURS_END=19        # 7 PM
#   SLOT_MINUTES=60
#   TIMEZONE=Asia/Kolkata
```

Functions:
- `get_busy_periods(days_ahead: int) -> list` — call Calendar `freebusy().query()`
  for the shared calendar over the next N days.
- `generate_candidate_slots(days_ahead: int) -> list[datetime]` — every slot in
  business hours, skipping Sundays, skipping slots less than 2 hours from now.
- `get_free_slots(days_ahead=5, limit=5) -> list[dict]` — candidates minus busy,
  each `{"index": 1, "start_iso": ..., "label": "Thu 24 Jul, 11:00 AM"}`.
- `create_event(slot_iso, customer_name, customer_phone, advisor_email, notes)`
  → creates the event with the advisor as attendee, `sendUpdates="all"` so Google
  emails them. Returns the Google event id.

**Important:** service accounts cannot invite attendees on personal calendars
without domain-wide delegation. Since we own the shared calendar and simply add
attendees, use `sendUpdates="all"`. If Google rejects attendee invites, fall back
to putting advisor details in the event description and log a warning — do not crash.

### 2. `appointments_db.py` (new)

SQLite, schema per the architecture doc:

```sql
CREATE TABLE IF NOT EXISTS appointments (
  appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_phone     TEXT,
  lead_name      TEXT,
  advisor_email  TEXT,
  property_ref   TEXT,
  google_event_id TEXT,
  slot_start     TEXT,
  appt_type      TEXT,      -- 'site_visit' | 'advisor_call'
  status         TEXT,      -- 'confirmed' | 'cancelled'
  created_at     TEXT
);
CREATE TABLE IF NOT EXISTS pending_slots (
  lead_phone TEXT PRIMARY KEY,
  slots_json TEXT,
  created_at TEXT
);
```

`pending_slots` is essential: when we show the user a numbered list, we must
remember which slot each number meant so `/book-slot` can resolve their reply.
Key it on the customer's phone number.

Functions: `save_pending_slots`, `get_pending_slots`, `save_appointment`,
`next_advisor()` (round-robin — store a counter or use `COUNT(*) % 3`).

### 3. New endpoints in `app.py`

**`POST /available-slots`**

Request (from WATI):
```json
{ "phone": "@phone", "appt_type": "site_visit" }
```
Behaviour: get free slots, save them to `pending_slots` for this phone, return a
numbered text block.

Response:
```json
{
  "slots_text": "1. Thu 24 Jul, 11:00 AM\n2. Thu 24 Jul, 2:00 PM\n3. Fri 25 Jul, 10:00 AM",
  "has_slots": "yes",
  "slot_count": 3
}
```
If no slots: `has_slots: "no"` and `slots_text` explaining an advisor will call.
Return `has_slots` as the **string** "yes"/"no" — WATI condition nodes compare
strings and booleans have failed before.

**`POST /book-slot`**

Request:
```json
{ "phone": "@phone", "choice": "@slot_choice", "name": "@name", "appt_type": "site_visit" }
```
Behaviour:
1. Load pending slots for the phone.
2. Parse `choice` — accept "2", "option 2", "2nd". If unparseable or out of range,
   return `booked: "no"` with a helpful `message`, do NOT crash.
3. Pick advisor round-robin.
4. Create the calendar event.
5. Save to `appointments`.
6. Clear pending slots.

Response:
```json
{
  "booked": "yes",
  "message": "Appointment confirmed for Thu 24 Jul, 2:00 PM. Arpit from Indihomes will meet you.",
  "advisor": "Arpit ",
  "slot_label": "Thu 24 Jul, 2:00 PM"
}
```

Keep all response values **flat strings** — WATI cannot read nested JSON reliably.

### 4. Error handling

Every endpoint must degrade gracefully. If Google Calendar is unreachable or
credentials are missing, return `has_slots: "no"` / `booked: "no"` with a message
saying an advisor will call to arrange the visit. Never return a 500 to WATI — a
crash mid-conversation is worse than a fallback message.

Add `GET /health` fields: `calendar: "connected"` or `"NOT CONFIGURED"`, and
`advisors_loaded: 3`.

## Testing (do this before declaring done)

```powershell
# 1. Health shows calendar connected
Invoke-RestMethod -Uri "http://localhost:8000/health"

# 2. Slots come back numbered
Invoke-RestMethod -Uri "http://localhost:8000/available-slots" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999999","appt_type":"site_visit"}'

# 3. Booking works
Invoke-RestMethod -Uri "http://localhost:8000/book-slot" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999999","choice":"2","name":"Test User","appt_type":"site_visit"}'

# 4. Booking the same slot twice must not double-book
# 5. Invalid choice ("9") returns booked:"no" with a friendly message, not a crash
```

Also verify the event actually appears in Google Calendar and the advisor
received an email.

## Style notes

- Match the existing code style: plain functions, no classes unless needed,
  defensive `.get()` everywhere, docstrings explaining WHY not what.
- All user-facing strings should sound like Rachna: warm, plain, no emoji,
  no exclamation-mark spam. Match the tone in `property_core.py`.
- Do not modify `property_core.py` or `llm_location.py` for this task.

---

# TASK: Free-text handling ("users typing instead of tapping buttons")

## The problem, from a real production conversation

A lead (Megha) went through the whole qualification flow correctly - area,
configuration, budget, flexibility, purpose, priority - and got a shortlist
of 3 properties. Then:

```
[Bot] Which one would you like to see in detail? Reply with its number.
[Megha] No one
[Bot] Please reply with a number between 1 and 3 to see that property.
[Bot] Would you like one of our property advisors to help you explore these options further?
[Megha] Send in borivali east also
[Megha] Not Right Now
```

Both typed replies were real, high-intent signals - "none of these fit" and
"show me a different area too" - and both were mishandled. "Send in borivali
east also" landed on a static yes/no button node with no webhook behind the
"no" path, so it silently fell through and the very next thing on record is
"Not Right Now", which reads in the CRM as an advisor decline from someone
who was actually still actively shopping.

**Root cause:** WATI's visual chatflow graph is edges = button clicks. A
typed reply that isn't one of the button labels (or, at a numbered-list
node, isn't a valid number) has nowhere to go in the graph.

## What was built

### 1. `intent_router.py` (new)

A small, dependency-free, **rule-based** classifier - no LLM call, no
network, deterministic. `classify(text)` looks for five GLOBAL intents that
can appear at almost any point in the conversation:

| Intent | Example phrases | 
|---|---|
| `stop` | "stop", "unsubscribe", "don't message me" |
| `talk_to_advisor` | "talk to someone", "call me", "connect me to an agent" |
| `reject_all` | "no one", "none of these", "not interested in these" |
| `change_location` | "send in borivali east also", "kandivali east" (bare) |
| `restart` | "start over", "restart" |

Anything else returns `{"intent": "none"}` and the caller falls back to
whatever local handling that endpoint already had - **no behaviour changed
for the common case** (a valid button tap or a valid number still works
exactly as before).

Why rule-based and not an LLM call: these five intents are each expressed in
a small, closed set of ways in real WhatsApp replies, a keyword/regex tier
answers them for free and instantly, and - critically for `stop` - a
compliance-relevant decision should never depend on "what the model felt
like today". Location extraction re-uses the exact same whitelist
(`llm_location.normalize_location` / `property_core.KNOWN_LOCALITIES_LOWER`)
the rest of the app already trusts; this module only adds the "is this even
a location-change request" trigger, never new location logic. See the
module docstring for the full design rationale and a note on adding an LLM
tier later if logs show the rule-based tier is missing real traffic.

**How to add a new phrase:** open `intent_router.py`, find the matching
`_STOP_PHRASES` / `_ADVISOR_PHRASES` / `_REJECT_PHRASES` / `_RESTART_PHRASES`
list, add the lowercase phrase. No other change needed - `classify()` checks
substrings, so "call me" also catches "can u call me pls".

### 2. `appointments_db.py` - `opted_out` table (new)

`mark_opted_out(phone)` / `is_opted_out(phone)` / `opted_out_count()`.
Deliberately separate from `conversation_tracker`'s `closed` status:
closing a conversation only stops the *current* 2-hour follow-up nudge; a
fresh `/search` reopens a new tracking row. `opted_out` is a permanent
do-not-contact flag any proactive send - the follow-up scheduler today,
anything else later - must check regardless of conversation state.

### 3. `followup_scheduler.py` - opt-out check (patched)

Before every sweep sends a re-engagement message, it now checks
`appointments_db.is_opted_out(phone)` first and skips (closing the row) if
so. This is the actual enforcement point for `stop` - classifying the
intent is useless without something that respects it.

### 4. `app.py` - `_run_global_intent()` + two entry points

`_run_global_intent(intent, req, phone)` executes the side effects for one
classified intent and returns a flat, WATI-friendly dict (`handled`,
`action`, `reply_text`, plus the usual `/search`-shaped fields for
`change_location`). It's the single place all five intents are handled, so
both entry points below share identical behaviour.

**Entry point A - `/property-detail` (patched, works today, no WATI
changes needed):** when the user's reply to "reply with its number" isn't a
valid number, it now runs `intent_router.classify()` on it *before* falling
back to the generic "please reply with a number" message. This directly
fixes the exact transcript above: "No one" -> `reject_all` -> a real offer
to widen the search or connect an advisor, instead of a repeated "reply
1-3". Because this webhook already exists and already fires at that node,
**this half of the fix is live the moment the backend is deployed** - no
WATI Builder change required.

**Entry point B - `POST /interpret-message` (new, needs WATI wiring):** the
general "any node, any time" fallback. Takes `phone`, `message`, and every
slot field the flow has collected so far (`location`, `configuration`,
`budget`, `purpose`, `amenities`, `builder`, `possession`, all optional) and
returns:

```json
{
  "intent": "change_location",
  "is_global": "yes",
  "handled": "yes",
  "reply_text": "Sure — here's what's available there:",
  "recommendations": "1. ...",
  "count": 3,
  "name1": "...", "detail1": "...", "image1": "...",
  "resolved_location": "Borivali East"
}
```

This is the endpoint that would have caught "Send in borivali east also" at
the advisor yes/no node in the transcript - but that node is a static WATI
button question with no webhook on the unmatched-reply path today. **Wiring
it up requires a change in WATI Builder, not just backend code** - see
"Required WATI wiring" below.

### 5. `/health`

Now reports `opted_out_count` alongside the existing fields, so you can see
at a glance whether the stop path is actually being hit in production.

## Required WATI wiring (for full "any point in the workflow" coverage)

The `/property-detail` fix is live with no WATI changes. To get the SAME
protection everywhere else in the flow (the advisor yes/no node, the
possession/builder questions, the budget-flexibility question, etc.), WATI
Builder needs:

1. Open the flow in WATI Builder.
2. Find the flow-level **"No Match" / "Fallback"** handling (WATI calls this
   the default path a message takes when it doesn't match any button or
   keyword at the CURRENT node - check under the node's settings or the
   flow's global fallback, depending on your WATI plan).
3. Point that fallback to a **new webhook node** calling
   `POST {{base_url}}/interpret-message` with body:
   ```json
   {
     "phone": "@phone",
     "message": "@whatsappMessage",
     "location": "@location",
     "configuration": "@configuration",
     "budget": "@budget",
     "purpose": "@purpose",
     "amenities": "@amenities",
     "builder": "@builder",
     "possession": "@possession"
   }
   ```
   (Use whatever custom-attribute names the flow already stores these
   under - see the existing `/search` node's webhook body for the exact
   names in use.)
4. Add a **Condition node** after it on `is_global`:
   - `is_global` Equal `yes` -> send `{{reply_text}}` as a text message,
     then (for `change_location` specifically) also send
     `{{recommendations}}` / `{{name1}}` etc. the same way the existing
     `/search` node's success path does, and route back into the flow at
     the property-picker step.
   - `is_global` Equal `no` -> fall through to whatever "I didn't
     understand, please tap a button" copy that node already had.

This is a one-time flow change; the endpoint itself needs no further work
to support it. Because WATI's fallback routing varies by plan/version,
verify the exact "no match" configuration against your actual WATI account
before wiring this (same caution as the rest of this document).

## Known limitations (v1, by design)

- **Rule-based only, no LLM tier.** Hinglish or indirect phrasing ("kuch
  aur dikhao", "ye theek nahi hai") won't be caught yet. Watch
  `/interpret-message`'s `intent: "none"` rate in logs; if a specific
  phrasing shows up repeatedly, either add it to the keyword list (cheap) or
  revisit adding an LLM tier (see `intent_router.py`'s docstring).
- **`reject_all` doesn't auto-widen the search.** It offers the choice back
  to the customer rather than guessing what would fit - deliberate, but
  worth revisiting if data shows most people just want the same criteria
  with a different area.
- **A bare "no" is never read as `reject_all`**, on purpose - it's a normal
  answer to unrelated yes/no questions elsewhere in the flow (e.g. "is that
  a firm budget?"). This means a genuinely short rejection like "no" (with
  nothing else) at the property-picker still falls through to the generic
  retry message. Acceptable tradeoff; do not "fix" this without checking
  what else in the flow would break.
- **`/interpret-message` needs the WATI-side wiring above** to cover nodes
  other than `/property-detail`. Until that's wired, only the property-
  picker node is protected.

## Testing (do this before declaring done)

Automated (no live WATI/Groq needed - `intent_router.py` is pure rules, and
the app-level tests mock `property_core.search`):

```powershell
cd C:\Users\admin\Desktop\Indihomes-chatbot-V1
python -m unittest tests.test_intent_router -v
```

Manual, against a running `uvicorn app:app` (PowerShell, matching the style
used elsewhere in this file):

```powershell
# 1. Health shows the new opted_out_count field
Invoke-RestMethod -Uri "http://localhost:8000/health"

# 2. Reproduce "No one" at the property-picker (Entry point A - works today)
#    First save a shortlist for a test phone via a real /search call, then:
Invoke-RestMethod -Uri "http://localhost:8000/search" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999998","location":"Kandivali East","configuration":"2 BHK","budget":"1 Cr - 2 Cr"}'

Invoke-RestMethod -Uri "http://localhost:8000/property-detail" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999998","choice":"No one"}'
# Expect: intent = "reject_all", is_global = "yes", detail offers to widen / advisor.

# 3. Reproduce "Send in borivali east also" at the SAME node
Invoke-RestMethod -Uri "http://localhost:8000/property-detail" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999998","choice":"Send in borivali east also"}'
# Expect: intent = "change_location", is_global = "yes", a fresh recommendations block for Borivali East.

# 4. A genuinely unparseable, non-global reply must be unchanged (no regression)
Invoke-RestMethod -Uri "http://localhost:8000/property-detail" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999998","choice":"maybe the blue one"}'
# Expect: intent = "none", is_global = "no", the original "reply 1-3" message.

# 5. The generic fallback endpoint (Entry point B) directly
Invoke-RestMethod -Uri "http://localhost:8000/interpret-message" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999997","message":"talk to an advisor please"}'
# Expect: intent = "talk_to_advisor", is_global = "yes", reply_text confirms an advisor will contact them.

# 6. Opt-out actually sticks
Invoke-RestMethod -Uri "http://localhost:8000/interpret-message" -Method Post `
  -ContentType "application/json" -Body '{"phone":"919999999996","message":"please stop messaging me"}'
Invoke-RestMethod -Uri "http://localhost:8000/health"
# Expect: opted_out_count went up by 1.

# 7. Empty / garbage body must never 500
Invoke-RestMethod -Uri "http://localhost:8000/interpret-message" -Method Post `
  -ContentType "application/json" -Body '{}'
```

Also worth doing once: after wiring the WATI fallback node (see above),
replay the exact Megha transcript from a real WhatsApp number and confirm
"Send in borivali east also" now returns real Borivali East listings instead
of being silently dropped.

---

# CHANGELOG: WATI flow updated + a bug caught before deploy

## Bug found and fixed: `reply_text` was silently swallowing `recommendations`

While wiring the `/property-detail` global-intent branch into the real WATI
flow (see below), a bug was caught in `_run_global_intent()`'s
`change_location` handler before it ever reached a live customer.

**The bug:** `change_location` sets TWO fields on its result - `reply_text`
(a one-line intro, e.g. `"Sure - here's what's available there:"`) and
`recommendations` (the actual property listings from the fresh search).
`/property-detail`'s fallback combined them with:
```python
"detail": result.get("reply_text") or result.get("recommendations") or fallback
```
Since `reply_text` is always truthy when set, `or` short-circuits on the
first value and **never reaches `recommendations`**.

**Impact:** only the `change_location` path - i.e. only when someone typed
a new area (like "Send in borivali east also") at the property-picker
node. The customer would have seen just the intro line with no listings
after it. Every other intent (`stop`, `talk_to_advisor`, `reject_all`,
`restart`) only ever sets `reply_text`, so they were unaffected.

**The fix**, in `app.py`'s `/property-detail`:
```python
detail_parts = [p for p in (result.get("reply_text", ""),
                             result.get("recommendations", "")) if p]
detail_text = "\n\n".join(detail_parts) or \
    f"Please reply with a number between 1 and {len(items)} to see that property."
```
Both parts now show, joined by a blank line. Covered by a permanent
regression test - `test_change_location_at_property_detail_runs_a_fresh_search`
in `tests/test_intent_router.py` asserts both the intro AND the mocked
project name are present in `detail`.

**Lesson for future intent handlers:** if a handler in `_run_global_intent`
ever needs to return more than one piece of user-facing text, don't reach
for `or` to collapse them into a single field - `or` picks the first
truthy value and discards the rest. Combine explicitly (list + join, like
above) so every present field actually reaches the customer.

## WATI flow updated and imported

The production flow export (`Indihomes-main(prod)_1`) was parsed directly
(not worked from assumptions) and patched with the two changes below, then
re-imported into WATI Builder. Both map to real node IDs in the flow.

**Change 1 - `main_webhook-HFQPC` (`/property-detail`)**

Now also captures `intent` and `is_global` as response variables
(`prop_intent`, `is_global`). A new condition node,
`main_condition-propintent`, checks `@prop_intent == "change_location"`:
- true -> loops back to `main_question-sKKYv` (the property-picker
  question) so the customer immediately sees a fresh numbered list for the
  new area they asked about.
- false -> unchanged, falls through to the existing `main_message-SnfCo`
  (which now correctly shows the combined `reply_text` + `recommendations`
  for every other intent, or the real property detail, thanks to the bug
  fix above).

**Change 2 - `main_buttons-next`** ("Book a Site Visit / Talk to an Advisor
/ Not Right Now" - the exact node "Send in borivali east also" hit in the
Megha transcript)

Its `interactiveButtonsDefaultNodeResultId` (previously empty on every
button node in the flow) is now set to a new webhook node,
`main_webhook-interpret`, which calls `POST /interpret-message` with the
customer's raw typed text plus every slot collected so far. Downstream:
- `main_condition-intloc` checks `@intent == "change_location"` -> shows
  the intro + fresh listings (`main_message-intshow`), then loops into
  `main_question-sKKYv` just like Change 1.
- `main_condition-intglobal` checks `@is_global == "yes"` for everything
  else -> shows `{{reply_text}}` (`main_message-intreply`) if a global
  intent was recognised, or a generic "didn't catch that, please tap a
  button" (`main_message-intfallback`) if not - both loop back to
  `main_buttons-next` to re-ask the same question.

**One flagged uncertainty, now resolved by import:** `interactiveButtonsDefaultNodeResultId`
had no existing working example anywhere in the original flow export to copy
the edge-wiring convention from, so its wiring was inferred from how normal
button taps wire (item `nodeResultId` + matching edge). The import has been
done - **visually confirm in WATI Builder that the default (no-match) path on
`main_buttons-next` actually draws a connecting line to `main_webhook-interpret`**
before relying on it in production; this is the one part of the update that
wasn't backed by a working precedent in your file.

## Deployment status

- Backend fix (`app.py`): written, unit-tested (22/22 passing including the
  new regression test), ready to deploy.
- WATI flow: updated JSON imported into Builder.
- **Before going live:** re-run `python -m unittest tests.test_intent_router -v`
  one more time against the current `app.py` to confirm the fix, then do the
  visual check on `main_buttons-next`'s default path described above, then
  redeploy the backend so the running server matches what the imported flow
  now expects (`intent` / `is_global` on the `/property-detail` response,
  and the `/interpret-message` endpoint existing at all).
