"""
Conversation-level lock.

Prevents two events for the SAME phone number from being processed at the
same time - rapid double-taps on a WATI button, a WATI webhook retry after a
slow response, or a duplicated network call. Keyed on phone because the race
is always "one user, two near-simultaneous events" - two different users are
always safe to process in parallel and are never serialized against each
other.

Responsibilities of this module, and ONLY these:
    acquire(phone)   - try to take the lock for a phone number
    release(phone)   - release it
    cleanup()        - drop stale entries (garbage collection, not required
                        for correctness - acquire() already self-heals)

No business logic lives here. This module has no idea what a "search" or a
"priority" is - it only tracks "is this phone mid-request right now". The
decision of WHAT to do when a lock can't be acquired (what fallback message
to send, which endpoint cares) belongs to app.py, not here.

Concurrency model: single in-memory dict guarded by a threading.Lock.
This is correct for exactly the deployment this project runs (see
Procfile: `uvicorn app:app`, no --workers, no gunicorn). uvicorn's default
run is one process, and within that process FastAPI's sync endpoints are
already serialized onto threads that share this module's memory, so a plain
dict + lock is enough - no Redis needed for Phase 1.
If this backend is ever run with multiple worker processes (`--workers N`)
or multiple machines, this in-memory dict stops being shared across them
and the lock silently stops working across processes. At that point, swap
the dict below for Redis `SET key val NX EX <timeout>` - the acquire/release
call sites in app.py would not need to change, only this file.
"""

import threading
import time
from typing import Dict

# Guide range is 2-5 seconds; default to the upper bound so a slow but
# legitimate request (e.g. a live Groq call inside /search) doesn't get its
# own lock expire out from under it while it's still genuinely working.
DEFAULT_TIMEOUT_SECONDS = 5.0

_guard = threading.Lock()               # protects _locks itself, not the phones
_locks: Dict[str, float] = {}           # phone -> monotonic timestamp when the lock expires


def acquire(phone: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Try to acquire the lock for `phone`.

    Returns True if the lock was acquired - the caller now owns it and MUST
    call release(phone) when done (use try/finally).
    Returns False if another request for this phone is already holding the
    lock - the caller should skip processing and return a fallback response.

    A phone with no value is let through unconditionally (True): there is
    nothing to key a lock on, and refusing to process would silently drop a
    real user's message rather than just fail to deduplicate one.

    A lock past its own expiry is treated as abandoned (e.g. a previous
    request crashed before reaching `finally: release(...)`) and is
    silently reclaimed here - this is what makes the lock self-healing
    without needing a background sweep.
    """
    if not phone:
        return True

    now = time.monotonic()
    with _guard:
        expires_at = _locks.get(phone)
        if expires_at is not None and expires_at > now:
            return False
        _locks[phone] = now + timeout_seconds
        return True


def release(phone: str) -> None:
    """Release the lock held for `phone`. Safe to call even if no lock is
    currently held (e.g. phone was empty, or it already expired) - this is
    always called from a `finally` block and must never itself raise."""
    if not phone:
        return
    with _guard:
        _locks.pop(phone, None)


def cleanup() -> int:
    """Drop expired lock entries from memory. Purely a housekeeping call for
    a long-running process - acquire() already ignores expired entries on
    its own, so correctness never depends on cleanup() being called. Returns
    how many entries were removed, mainly so a caller can log/monitor it."""
    now = time.monotonic()
    with _guard:
        expired = [phone for phone, expires_at in _locks.items() if expires_at <= now]
        for phone in expired:
            del _locks[phone]
        return len(expired)
