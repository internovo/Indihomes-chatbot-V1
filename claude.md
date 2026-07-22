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
  "message": "Appointment confirmed for Thu 24 Jul, 2:00 PM. Rahul from Indihomes will meet you.",
  "advisor": "Rahul Shah",
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