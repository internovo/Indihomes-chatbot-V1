# understand.md — Phase 1 (the reactive WhatsApp bot)

> **Read this like a whiteboard session, not a reference manual.** It builds
> up from the problem, one idea at a time. By the end you should be able to
> defend every design decision in this codebase in a production review.
> Companion file: `../Indiihomes-chatbot-phase2/understand.md` (the other
> half of the system). And `claude.md` in this same folder is the running
> changelog of *decisions* — this file is the mental model behind them.

---

## 1. What problem are we actually solving?

A real scene. Megha visits Indihomes, opens WhatsApp, and messages the
Indihomes number: *"Hi, I'm interested."* She wants a 2 BHK in Kandivali
East, budget ₹1–2 Cr.

The **painful manual version**: a salesperson has to be awake and at their
phone, ask her the same 8 questions every lead gets asked (area? budget?
BHK? own-use or investment?), look up matching projects in a spreadsheet,
type them out, and book a site visit — for every single lead, at any hour.

What we're building automates that qualification conversation: it asks the
questions, understands the answers (even when she types instead of tapping
a button), pulls matching live inventory, shows her properties, books a
site visit on a real calendar, and hands a warm lead to a human advisor —
without a human being awake for step one.

---

## 2. The single most important idea in the whole project

**WATI is not the application. WATI is the communication layer.**

If you only remember one thing, remember this. Everything else follows from
it.

The honest one-line mental models:

- **WATI = Mouth + Ears.** It sends and receives WhatsApp messages. That's
  it.
- **Your FastAPI backend = the Brain.** It decides what to say, looks things
  up, makes every real decision.
- **SQLite + the live Indihomes API = Memory.** The source of truth for what
  a lead said and what inventory exists.

```
Customer (Megha)
     │  types on WhatsApp
     ▼
WhatsApp (Meta)         ← the actual message pipe
     │
     ▼
WATI                    ← Mouth + Ears: receives, and later speaks
     │  webhook call (HTTP POST) at each step of the flow
     ▼
Your FastAPI backend    ← the Brain: every decision happens here
     │
     ├──► SQLite (appointments.db)      ← Memory: shortlists, bookings, opt-outs
     ├──► Live Indihomes API            ← Memory: the actual property inventory
     └──► Google Calendar / Email       ← booking + advisor notification
```

**Why this separation is the whole ballgame:** if Indihomes ever swaps WATI
for Interakt, Gupshup, or Meta's Cloud API directly, *only the top box
changes.* The Brain and the Memory are untouched. That's the hallmark of a
system that won't need a rewrite next year.

### Who owns what

| Component | Responsibility |
|---|---|
| WhatsApp (Meta) | Delivers messages between Megha and the business |
| WATI | The visual chatflow, buttons, templates, contacts, and the webhook calls into our backend |
| **FastAPI backend (`app.py`)** | **Every decision: understand location, search inventory, book slots, classify free text, gate business hours** |
| SQLite (`appointments.db`) | Per-phone state: the shortlist we showed, pending slots, bookings, opt-outs, follow-up timers |
| Live Indihomes API (`property_api.py`) | The real, current property inventory |
| Google Calendar / Brevo email | Site-visit events and advisor notifications |

---

## 3. How a single conversation actually flows

The WATI chatflow is a graph of nodes. Most nodes are buttons. At a few key
points, a node makes an **HTTP POST to our backend** (a "webhook node"),
waits for the JSON response, and speaks whatever text that JSON contains.

That request/response handshake is the heartbeat of the whole system.
Let's trace Megha's conversation and name the endpoint behind each step:

```
Megha: "Hi, interested"        → WATI static welcome, buttons
Megha: taps area / types area  → POST /location    (understand the area)
Megha: taps "2 BHK"            │
Megha: taps "1 Cr - 2 Cr"      │  (WATI collects these as it goes)
Megha: taps "Own Use"          │
Megha: taps priority           │
   ...                         ▼
[flow reaches the end]         → POST /search       (find + show properties)
Megha: "1"                     → POST /property-detail  (show that project)
Megha: taps "Book Site Visit"  → POST /available-slots  (show real free slots)
Megha: "2"                     → POST /book-slot     (book it on the calendar)
[booking done]                 → POST /save-lead     (write lead to CRM)
```

Each arrow is a webhook. Each webhook is a function in `app.py`. The pattern
is always the same: **WATI sends what it has, the backend returns flat JSON,
WATI displays fields from that JSON.**

### The one shape rule that makes this work

Every backend endpoint returns a **flat dict of strings**, because WATI's
variable system can only read flat top-level fields — `@recommendations`,
`@count`, `@name1`. It cannot dig into nested JSON. So you'll never see a
deeply-nested response in this codebase; everything is deliberately flattened
into `name1`, `detail1`, `image1`, `name2`, … precisely so a WATI Message
node can print `{{name1}}`.

---

## 4. How we list properties (the search)

This is `property_core.py`, the heart of the "Memory → answer" path.

**Step 1 — where does inventory come from?** The live Indihomes API
(`property_api.py`), refreshed on a TTL. `properties.json` is kept only as an
*offline fallback* so the bot never dies if the API blips:

```python
def load(force=False):
    raw = property_api.fetch_all(force=force)   # live API first
    if raw:
        _rebuild(raw); return
    if not PROPERTIES:                          # only if we have nothing at all
        _load_offline()                         # fall back to properties.json
```

**Step 2 — normalize the messy live data into one clean internal shape.**
The live API has quirks (price sometimes 0 in one field, carpet sizes as
strings *or* numbers, media as `["url"]` *or* `[{"url","tag"}]`). `_normalize()`
is the single place that irons all of that out, so `search()` never has to
think about API weirdness:

```python
def _normalize(r):   # live API record → internal shape
    return {
        "price_cr": lakh_to_cr(r["startingPrice"]["value"]),  # ALWAYS startingPrice
        "configs": [c.lower().replace(" ","") for c in r["flatConfiguration"]],
        "carpet": _normalize_carpet(r["carpetSize"]),         # str-or-number → float
        ...
    }
```

**Step 3 — `search()` filters in plain Python** over that clean list. No
database query language, no external search service — the inventory is small
enough that filtering in memory is simpler and fast:

```python
def matches(p):
    if not loc_ok(p):          return False   # right area?
    if cfg not in p["configs"]: return False   # right BHK?
    if p["price_cr"] > ceiling: return False   # within budget?
    return True

results = [p for p in PROPERTIES if matches(p)]
results.sort(key=amenity_score, reverse=True)  # best amenity match first
top = results[:limit]
```

**Step 4 — return BOTH human text and machine data.** `recommendations` is
the numbered list Megha reads; `shortlist` is a structured list saved to
SQLite so that when she replies "1", `/property-detail` can resolve it
*without a second API call*:

```json
{
  "recommendations": "1. Sethia Pride\nNear Mahindra Gate\n1BHK / 2BHK, starting 0.98 Cr\n...",
  "count": 3,
  "name1": "Sethia Pride", "detail1": "...", "code1": "INV_KDE_608",
  "shortlist": [{"index": 1, "code": "INV_KDE_608", "detail": "<full block>", "image": "..."}]
}
```

That "save the shortlist so the next reply can resolve a number" trick is
worth internalizing — it's why the bot can say "reply with a number" and
actually know what number 2 *was*.

---

## 5. Where APScheduler is, and why

**APScheduler = a cron job that lives inside your Python process.** No
external cron server, no Railway add-on — just a background thread that wakes
up on a timer and runs a function.

Phase 1 uses it for **one** thing: the **2-hour follow-up nudge**
(`followup_scheduler.py`).

The problem it solves: Megha gets her 3 properties, then goes quiet — didn't
say no, just got distracted. A good salesperson would nudge her a couple
hours later. APScheduler is how the bot does that without a human watching a
clock.

```
every 5 minutes (APScheduler BackgroundScheduler)
        │
        ▼
run_followup_sweep()
        │
        ├─ ask conversation_tracker: who got recommendations 2h+ ago
        │  AND hasn't replied since AND hasn't already been nudged?
        │
        ├─ for each such phone:
        │     ├─ opted out ("stop")?  → skip forever
        │     ├─ locked (mid-reply right now)? → skip this tick, catch next
        │     └─ else → send the re-engagement WhatsApp, mark it sent
        ▼
```

Note it sweeps **every 5 minutes** but only nudges someone whose **2-hour**
window has elapsed — the 5-min tick is just how often it *checks*; the 2h is
the actual wait. Two settings that matter for a Railway deploy:

- `coalesce=True` — if the process restarts and two ticks pile up, run once,
  not twice. **Prevents double-nudging on redeploy.**
- `max_instances=1` — never two sweeps at once, even if WATI is slow.

Phase 2 uses APScheduler much more heavily (the 45-second lead poll, the
daily flush) — see that folder's `understand.md`.

---

## 6. The APIs we expose (the backend's public surface)

Every one of these is a webhook a WATI node calls. Grouped by job:

**Understanding & searching**
- `POST /location` — takes free-text area ("Kandivali", "Thane mulund"),
  runs it through Groq (an LLM) to normalize to a real locality, asks a
  clarifying question if ambiguous ("Kandivali East or West?").
- `POST /search` — the big one. Understands location + filters inventory +
  returns the numbered shortlist. Saves the shortlist to SQLite.
- `POST /property-detail` — resolves "1" / "2" / "3" against the saved
  shortlist, returns the rich single-property block. **Also** the place free
  text like "No one" or "send Borivali East" gets caught (see §7).

**Booking**
- `POST /available-slots` — pulls real free slots from Google Calendar.
- `POST /book-slot` — books the chosen slot, emails the advisor, closes the
  follow-up timer.

**Lead handling & free text**
- `POST /interpret-message` — the general "user typed something unexpected"
  catch-all (see §7).
- `POST /advisor-request` — "Talk to an Advisor" button → email advisors,
  close conversation.
- `POST /save-lead` — write the finished conversation to the CRM.
- `POST /parse-priorities` — turn "1 and 3" into flat yes/no flags WATI can
  branch on.

**Ops**
- `GET /health` — is everything wired? Now also reports `business_hours` and
  `opted_out_count`.

---

## 7. The two hard problems we hit in production (and how we fixed them)

These are the war stories. They're in `claude.md` in full; here's the
*understanding* behind them.

### Problem A — "users type instead of tapping buttons"

**The trap:** WATI's chatflow is a graph where edges = button taps. When
Megha *typed* "No one" and "Send in Borivali east also" instead of tapping,
those messages hit nodes that only knew about buttons — and fell into a dead
end. In the real transcript, a genuine buying signal ("show me Borivali too")
was silently dropped and logged as if she'd declined an advisor.

**The mental model of the fix:** a button tap is just a message whose text
happens to equal a button label. So treat *every* inbound message the same
way — classify it before assuming it's a button.

We built `intent_router.py`: a small, **rule-based** (not LLM) classifier
that catches five "global intents" — things that mean the same thing no
matter what question was asked:

```
Any inbound free text
        │
        ▼
intent_router.classify(text)
        │
   ┌────┴─────┬──────────┬───────────────┬──────────┐
   ▼          ▼          ▼               ▼          ▼
 stop    talk_to_    reject_all    change_location  restart
        advisor    ("no one")   ("send Borivali")
```

**Why rule-based, not an LLM?** Five intents, each said a handful of ways.
Rules are free, instant, and — critically for `stop` (compliance) —
*deterministic*. An LLM belongs later, only if logs show real phrasings the
rules miss. (This is the "80% deterministic, 20% AI" principle: don't route
every message through an LLM in a sales flow — it's expensive, unpredictable,
and hard to debug.)

### Problem B — the WATI variable-syntax bug that ate our listings

**The trap:** WATI has *two* different variable namespaces that look almost
identical:
- Flow Variables → written `@intent`
- Contact Attributes → written `{{intent}}`

Which one a webhook response field becomes depends on how it's *mapped* in
WATI. We mapped `intent` as a Contact Attribute (`{{intent}}`) but wrote the
condition nodes checking `@intent` — a Flow Variable that **didn't exist**.
Every condition silently evaluated false. The backend was returning perfect
data; WATI was reading the wrong variable and routing to the fallback.

**The lesson that generalizes:** when the backend clearly returns the right
value but the bot behaves as if it didn't, the bug is almost always in the
WATI-side variable reference (wrong syntax, wrong type, or a missing key) —
**not** the backend. The way we found it was checking the contact's actual
attribute values in WATI right after a live message, not just reading the
visible reply.

A cousin of this bug: an early-exit path returned `reply_text` but forgot to
also set `recommendations`. Since `{{recommendations}}` is a *persistent*
Contact Attribute, WATI printed the literal text `{{recommendations}}` into
the chat for a contact that had never had it set. **Lesson:** any handler
that feeds a WATI template with multiple `{{placeholders}}` must set *every*
one of those keys on *every* return path, even as `""`.

---

## 8. Business-hours gating (the most recent feature)

**The problem:** the bot would happily reply and book things at 2 AM,
promising a human callback nobody could honor. Indihomes wanted replies
gated to 10 AM – 7 PM IST — but *without dropping* overnight leads.

**Why WATI can't do this itself:** its native business-hours setting only
governs WATI's *own* team inbox, not our custom webhook-driven flow. WATI has
no idea when our backend chooses to reply. So the gate has to live in our
code.

The shape in Phase 1 (`business_hours.py` + a gate in `app.py`):

```
inbound webhook (any customer-facing endpoint)
        │
        ▼
_off_hours_text(phone)
        │
   ┌────┴─────────────────────────┐
   ▼                              ▼
is_business_hours() == True     == False
   │                              │
proceed normally          return the off-hours notice
(no change at all)         (skip LLM, skip search, skip booking)
                                  │
                          first message today → full notice
                          repeat messages → short line
```

`is_business_hours()` is dead simple and worth reading once — it's the whole
gate:

```python
def is_business_hours(dt=None):
    dt = dt or datetime.now(IST)
    return time(10,0) <= dt.astimezone(IST).time() <= time(19,0)
```

The "once-per-day notice" is tracked on the same `conversation_activity` row
that already exists for the follow-up timer — no new table, per the design
doc.

**The Phase 1 vs Phase 2 split worth understanding:** Phase 1 is *reactive*
(a customer messaged us first), so "off hours" just means "reply with a
notice." Phase 2 is *proactive* (we message the lead first), so "off hours"
means "queue the lead and send at 10 AM" — a genuinely different mechanism.
Same `business_hours.py`, two different behaviors. See the Phase 2 doc.

---

## 9. Who owns what — the final table

| Layer | File(s) | Job |
|---|---|---|
| Communication | (WATI, external) | Speak/listen on WhatsApp; call our webhooks |
| Routing / decisions | `app.py` | Every endpoint; the Brain |
| Free-text understanding | `intent_router.py`, `llm_location.py` | Classify typed messages; normalize areas |
| Inventory | `property_core.py`, `property_api.py` | Load, normalize, search properties |
| Per-phone memory | `appointments_db.py`, `conversation_tracker.py` | Shortlists, slots, bookings, opt-outs, timers |
| Time gating | `business_hours.py` | The 10–7 IST window |
| Background timing | `followup_scheduler.py` (APScheduler) | The 2-hour nudge |
| Booking / notify | `calendar_service.py`, `email_service.py` | Calendar events, advisor emails |
| Safety | `conversation_lock.py` | One request per phone at a time |

---

## 10. One sentence, and the next move

**Phase 1 is a reactive WhatsApp qualification bot where WATI is only the
mouth and ears, every real decision lives in a FastAPI backend that
understands both buttons and free text, and per-phone memory in SQLite lets
a stateless chatflow behave like it remembers the conversation.**

**Next move if you want to go deeper:** open `app.py` and read
`_run_global_intent()` top to bottom — it's the single function where the
free-text feature, the business-hours gate, and the "return flat JSON WATI
can read" rule all meet in one place. If you understand that function, you
understand the spine of Phase 1.

---

## 11. The lead-safety-net — what happens when nobody taps a button

Section 7 already covered `intent_router.py` catching free text at ONE
node (`main_buttons-next`, the post-shortlist menu). This section is
about the much bigger version of that problem: **every other button
node in the flow had the same hole, and closing it turned into a real
production saga worth understanding end to end.**

### The problem, in one real transcript

A lead (Hitesh Tailor) hit this exact sequence:

```
Bot: Which area are you looking in?  [Goregaon] [Malad] [Other Area]
Hitesh: Malad
Hitesh: Goregaon        ← sent both in quick succession
Bot: Just to narrow it down - Malad East or Malad West?
Hitesh: Other Area      ← typed, didn't match either option
[bot goes silent — nothing happens]
```

Investigating turned up **two separate, genuinely different bugs**
wearing the same disguise:

1. The specific burst-message failure (rapid "Malad" then "Goregaon"
   landing on a stale clarify-question) was a real, narrow bug —
   already fixed in `llm_location.py` (an "Other Area" sentinel + a
   fresh-location override; see `claude.md`'s "burst-message location
   bug" entry).
2. The *general* version — **9 of the 10 `InteractiveButtons` nodes in
   the whole flow had an empty `interactiveButtonsDefaultNodeResultId`**
   — was still wide open. Only `main_buttons-next` (§7's story) had
   ever been wired to catch unmatched input. Free text or an emoji at
   `consent`, `config`, `budget`, `flex`, `purpose`, `priority`,
   `possession`, `wFpLC` (loan follow-up), or `RRfox` (closing menu)
   had nowhere to go — the conversation just stopped, and the CRM had
   no way to tell "this lead went cold on their own" apart from "the
   bot ate their message."

### Why this needed a WATI change, not just a backend change

This is the section 2 lesson ("WATI is not the application") pushed to
its edge case: **a backend fix alone cannot catch a message WATI never
forwards.** If a button node has no default path, WATI simply never
calls any webhook for an unmatched reply — there is no HTTP request
for `app.py` to even see. The fix necessarily has two halves: a WATI
flow change (give every button node a default path to call) and a
backend change (something for that path to call).

### The WATI import saga — a real lesson about "done" vs "deployed"

The first fix built was the *good* version: every empty default routes
to a dedicated webhook, all funneling into a shared
check-intent → maybe-loop-back-for-more chain, so a customer could keep
talking after a fallback instead of hitting a second dead end. It was
built, validated (node/edge graph checked programmatically for
dangling references), and genuinely correct — and then **WATI's
Builder repeatedly refused to import it**, with nothing more specific
than a generic "Request failed." Multiple rebuilds didn't fix it. The
diagnostic step that actually settled it: re-importing a file that had
*already imported successfully once before* also failed — conclusive
proof the block was environmental (a WATI session/service issue), not
anything in the JSON.

**The lesson that generalizes:** correct code (or a correct config
file) sitting on disk is not the same claim as "this is running in
production." Between "I built the fix" and "the fix is live" sits an
entire deployment mechanism that can fail for reasons that have
nothing to do with whether the fix is right. Don't skip verifying the
last mile.

### What's actually live right now

Because of that import wall, what's deployed today is an **earlier,
simpler draft** — `Indihomes-main_prod__v4_phase1-fallback.json`
(confusingly named; it predates the continuation-loop version
described above). It wires all 10 previously-empty defaults to
dedicated webhook nodes, but every one of them dead-ends at a single
shared acknowledgment message — no loop, no way to keep chatting
afterward. Simpler, and known to actually import, which turned out to
matter more than being maximally elegant.

```
any InteractiveButtons node's unmatched reply
        │
        ▼
main_webhook-fb-<node>  →  POST /lead-fallback
        │
        ▼
reply_text shown  →  conversation ends here (known limitation)
```

### `POST /lead-fallback` — matching the backend to the flow that's actually live

Rather than force yet another risky re-import, the backend was built
to match this simpler flow's *existing* webhook contract exactly
(`phone`, `name`, `last_step`, `raw_message`, `flow_node_id` in;
`reply_text`, `is_business_hours` out) — internally reusing the exact
same `intent_router.classify()` + `_run_global_intent()` logic
`/interpret-message` already used, so behavior (opt-out, advisor
handoff, `change_location`, `needs_human` logging) is identical
regardless of which endpoint a WATI node happens to call:

```python
intent = intent_router.classify(raw)

if intent["intent"] == "change_location":
    # this flow's message node has no separate {{recommendations}}
    # placeholder — combine both into one reply_text so real listings
    # still reach the customer, not just the intro line
    result = _run_global_intent(intent, req, phone)
    reply_text = "\n\n".join(p for p in (result["reply_text"], result["recommendations"]) if p)
elif intent["intent"] != "none":
    result = _run_global_intent(intent, req, phone)
    reply_text = result["reply_text"]
else:
    appointments_db.mark_needs_human(phone, name, last_step, raw)   # NEW
    reply_text = "Got it - I've noted that down and one of our property advisors will follow up with you shortly."
```

### `needs_human` — the CRM-visibility half of the fix

Before this, a genuinely unclassifiable message (an emoji, gibberish,
something none of the five intents matched) got logged nowhere. It
didn't crash, didn't show a blank message — it just vanished, and in
the CRM a lead that hit this looked *identical* to a lead who simply
never replied. `appointments_db.mark_needs_human()` gives it a real
record — `flow_step` (which node), `raw_message` (what they actually
typed), timestamped — surfaced via `GET /needs-human-leads` as an
advisor worklist, with `POST /needs-human-leads/ack` to clear handled
rows.

### The known, accepted gap

Because the *deployed* flow version dead-ends after one fallback
message (the continuation-loop version never made it past the import
wall), whatever a customer types *next* after a fallback still has
nowhere to go. This is tracked, not hidden — see `claude.md`'s
"Lead-safety-net" changelog for the full status — and would need
either a successful re-import of the loop version, or building the
loop into whatever flow version does eventually import cleanly.

---

## 12. The Phase 3 hook — notifying a salesperson, not just the customer

Everything in this file so far is about talking to the *customer*.
`/save-lead` now also fires a second, independent call:
`lead_routing_client.route_lead(lead)` — a best-effort POST to
`indihomes-lead-routing-service` (Phase 3), which resolves the
recommended project's salesperson in Cosmos and notifies *them* on
WhatsApp.

```
POST /save-lead
        │
        ├──► crm_service.push_lead()         (existing — writes the CRM record)
        └──► lead_routing_client.route_lead() (NEW — best-effort, never raises)
                     │
                     ▼
        indihomes-lead-routing-service (Phase 3)
```

**Currently a safe no-op in production**, on purpose: `LEAD_ROUTING_URL`
in this repo's `.env` is still a localhost placeholder
(`http://127.0.0.1:8080`) because the routing service hasn't been
deployed anywhere yet. The hook is written defensively — a failed
connection is caught, logged, and ignored — specifically so that
shipping this code early doesn't risk breaking `/save-lead` itself
while Phase 3 is still being finished. See
`../indihomes-lead-routing-service/understand.md` for that service's
own full design, and its `claude.md` for exactly what's still blocking
it from going live (deployment, a real Cosmos key, and confirming the
salesperson field names against a real document).

---

## 13. A second, sibling hook: lead-events, feeding indihomes-os's Lead Capture UI

Section 12 covers *notifying a salesperson*. A separate, later addition
covers something adjacent but different: *feeding the internal CRM's
"AI Activity" tick and "Lead Journey" tracker* so a human looking at a
lead in indihomes-os can see, at a glance, whether the WhatsApp bot
actually reached them and how far the conversation got — without
opening this bot's own logs.

Same mental model as §12's diagram, one more arrow:

```
POST /search, /property-detail, /advisor-request, /save-lead
        │
        └──► os_events_client.emit()   (NEW — best-effort, never raises)
                     │
                     ▼
        indihomes-os's POST /api/lead-events
        (drives the Lead Capture screen's AI Activity tick
         and Lead Journey vertical tracker)
```

Checkpoints emitted: `requirements_shared`, `options_shared`,
`detail_shared`, `advisor_requested`, `tagging_sent`, `opted_out` (from
`app.py`), plus `no_reply` and `followup_sent` (from
`followup_scheduler.py`'s existing 5-minute sweep — see that file's
`get_due_followups()`, which already computes "no reply within 2h" for
an unrelated reason and turned out to be exactly the signal this needed
too, so no separate poller was built).

**Same safety posture as §12's hook**: `os_events_client.py` mirrors
`lead_routing_client.py`'s shape almost exactly (dry-run default true,
never raises, urllib). Currently a no-op in practice — `OS_EVENTS_URL`
isn't set, and there's nothing to POST to yet regardless
(indihomes-os's own backend is itself incomplete right now — see that
repo's `structure.md`). See `claude.md`'s "Lead events" task section for
the full checkpoint-by-checkpoint writeup, including a real bug caught
and fixed while wiring `no_reply`'s idempotency key.

---

## 14. `{{recommendations}}` leaking on the MAIN flow, not just fallbacks

§11 and its "Bug 2" cousin (documented in `claude.md`) both cover an
unsubstituted `{{placeholder}}` leaking to a real customer because a
WATI Contact Attribute was never set for that contact. Both of those
were scoped to fallback/free-text paths, with an explicit earlier
claim that the MAIN `/search` → `main_message-recommend` path "was
never at risk", because `property_core.search()` unconditionally
returns a `recommendations` key on every code path. **That claim turned
out to be true about the DATA but not about the TIMING** — a real
production transcript showed the literal token "{{recommendations}}"
sent to a customer (Neeti Shukla) right after she completed the full
qualification flow.

### The actual mechanism

`property_core.search()` calls `_ensure_fresh()` first, which used to
work like this:

```python
def _ensure_fresh():
    ttl = property_api._cache_ttl()          # default 300s
    if (time.time() - _state["ts"]) > ttl:
        load(force=True)                     # BLOCKING, inline
```

`load(force=True)` → `property_api.fetch_all(force=True)` pages
through the ENTIRE live Indihomes catalogue — potentially several
sequential HTTP calls, each with its own `INDIHOMES_API_TIMEOUT`
(default 15s). Whichever customer's `/search` request happened to land
right as the 5-minute cache expired got stuck waiting on that whole
refresh, INLINE, before WATI ever saw a response. If that refresh ran
long enough, **WATI's own webhook timeout gave up first** — the
backend would still finish and return good data moments later, but too
late for WATI to capture it into the `recommendations` Contact
Attribute. The customer got the intro line, then the literal,
unsubstituted token.

This is a subtly different failure mode from Bug 2's "field was never
set on this contact" — here the field WOULD have been set, just not in
time. It only bites the unlucky request that lands exactly when the
cache is stale (rare, which is why it wasn't caught earlier), not
every request (which is why it looked safe under normal testing).

### The fix

`_ensure_fresh()` now NEVER blocks the caller on a live network call.
A stale cache (or the offline fallback) is served immediately; the
refresh runs on a daemon thread that updates `PROPERTIES` for the NEXT
search once it completes, guarded so a refresh already in flight isn't
duplicated:

```python
def _ensure_fresh():
    if (time.time() - _state["ts"]) <= ttl:
        return
    if _refresh_lock.acquire(blocking=False):
        if _state["_refreshing"]:
            return
        _state["_refreshing"] = True
        _refresh_lock.release()
    else:
        return
    threading.Thread(target=_background_refresh, daemon=True).start()
```

**The generalizable lesson:** "this endpoint always returns valid
data" and "this endpoint always returns FAST" are two different
claims. A webhook integration like WATI's cares about both — a
correct-but-slow response is, from the customer's side, indistinguishable
from no response at all. Any code path that can trigger a live network
call inline with a customer-facing request is a latent version of this
bug, regardless of whether the data it eventually returns is correct.
