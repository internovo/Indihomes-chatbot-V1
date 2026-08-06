"""
Background scheduler: sends follow-up WhatsApp messages to conversations that
have gone quiet after receiving property recommendations.

Trigger: every 5 minutes (APScheduler BackgroundScheduler, in-process).
No external cron. No Railway add-on. Works with the existing single-process
uvicorn deployment described in conversation_lock.py.

The sweep:
  1. Asks conversation_tracker for all rows where:
       - the 2-hour window has elapsed since the bot's last message
       - the user has NOT replied since then
       - a follow-up has not yet been sent
  2. For each eligible phone, acquires the conversation lock (so a sweep tick
     cannot race with a live user request for the same phone — they might be
     replying right now).
  3. Sends the re-engagement WhatsApp message via wati_client.
  4. On success, marks the row so it's never swept again.
  5. Releases the lock in a finally block.

APScheduler settings that matter for Railway:
  coalesce=True       — if two triggers pile up (slow restart, etc.) run once,
                        not twice. Prevents duplicate send on redeploy.
  max_instances=1     — never run two sweeps concurrently, even if one is
                        taking longer than the interval (e.g. WATI timeout).
  misfire_grace_time  — not set intentionally; a missed tick is just a tick,
                        not a crisis. The next tick will pick up any rows.

Timezone: Asia/Kolkata — matches the deployment location and the business hours
used in calendar_service.py, so log timestamps are readable by the Indihomes team.

Call start() exactly once, from app.py, right after `app = FastAPI()`.
It returns the scheduler so the caller can shut it down cleanly on SIGTERM if
needed (e.g. in a test teardown), but for normal production use you can ignore
the return value.
"""

import logging

import appointments_db
import conversation_lock
import conversation_tracker
import wati_client

logger = logging.getLogger(__name__)

# Set by start(); used by /health to confirm the scheduler thread is alive.
_scheduler = None


def _scheduler_running() -> bool:
    """True if start() was called and the APScheduler instance is still running."""
    return _scheduler is not None and _scheduler.running


def run_followup_sweep() -> None:
    """Single execution of the follow-up check. Called by the scheduler every
    5 minutes, but can also be called directly in tests.

    Each phone is processed independently: a lock failure or a WATI error on
    one phone does not skip the rest of the batch. Every failure is logged but
    never re-raised — the scheduler loop must survive any individual error.
    """
    try:
        due = conversation_tracker.get_due_followups()
    except Exception as e:
        logger.error("[followup_scheduler] could not query due followups: %s", e)
        return

    if not due:
        return  # nothing to do this tick — common case, skip the log noise

    if not wati_client.is_configured():
        logger.warning("[followup_scheduler] WATI not configured — skipping sweep")
        return

    logger.info("[followup_scheduler] sweep: %d phone(s) due for follow-up", len(due))

    for row in due:
        phone = row.get("lead_phone", "")
        name = row.get("lead_name", "") or ""

        if not phone:
            continue

        # Do-not-contact check FIRST, before acquiring the lock or touching
        # WATI at all - a phone that sent "stop" (see intent_router.py) must
        # never receive another proactive message, no matter what triggered
        # this sweep row. Close the row so it also stops reappearing here.
        if appointments_db.is_opted_out(phone):
            logger.info("[followup_scheduler] %s opted out - closing without sending", phone)
            try:
                conversation_tracker.close_conversation(phone)
            except Exception as e:
                logger.error("[followup_scheduler] could not close opted-out conversation %s: %s", phone, e)
            continue

        # Acquire the conversation lock before touching this phone.
        # If the user is mid-request right now (property detail, slot pick, etc.)
        # the lock is held by that request — we skip this tick and catch them
        # on the next 5-minute sweep instead.
        if not conversation_lock.acquire(phone):
            logger.info(
                "[followup_scheduler] %s is locked (live request in progress) — skipping this tick",
                phone,
            )
            continue

        try:
            ok = wati_client.send_followup_buttons(phone, name)
            if ok:
                conversation_tracker.mark_followup_sent(phone)
                logger.info("[followup_scheduler] follow-up sent and marked for %s", phone)
            else:
                logger.warning(
                    "[followup_scheduler] wati_client returned False for %s — will retry next sweep",
                    phone,
                )
        except Exception as e:
            # Belt-and-suspenders: wati_client is supposed to never raise, but
            # if something unexpected happens we must not crash the scheduler.
            logger.error("[followup_scheduler] unexpected error for %s: %s", phone, e)
        finally:
            conversation_lock.release(phone)


def start():
    """Create and start the background scheduler. Call once from app.py.

    Returns the running APScheduler instance so it can be shut down cleanly in
    tests. In production you can ignore the return value — the scheduler runs
    as a daemon thread and stops automatically when the process exits.

    If APScheduler is not installed (e.g. during a local dev run before
    pip install), this fails loudly at startup rather than silently at the
    first sweep — which is the right place to discover a missing dependency.
    """
    global _scheduler
    from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore

    scheduler = BackgroundScheduler(
        timezone="Asia/Kolkata",
        # Silence APScheduler's own verbose logging; we use our own log lines above.
        logger=logging.getLogger("apscheduler.scheduler"),
    )
    scheduler.add_job(
        run_followup_sweep,
        trigger="interval",
        minutes=5,
        id="followup_sweep",
        coalesce=True,        # merge missed triggers instead of stacking them
        max_instances=1,      # never run two sweeps at the same time
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[followup_scheduler] started; sweep every 5 minutes (Asia/Kolkata)")
    return scheduler
