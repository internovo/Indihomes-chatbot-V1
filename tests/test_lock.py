"""
Tests for conversation_lock.py.

Run:  python -m unittest tests.test_lock -v   (from the project root)
   or python test_lock.py                     (from inside tests/)

Plain unittest, no pytest, no network - matches the rest of tests/.
"""

import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conversation_lock as lock


class LockBasicsTests(unittest.TestCase):
    def setUp(self):
        # Each test gets its own phone number so tests can run in any order
        # without leftover locks from a previous test interfering.
        self._counter = getattr(LockBasicsTests, "_counter", 0) + 1
        LockBasicsTests._counter = self._counter
        self.phone = f"91900000{self._counter:04d}"

    def tearDown(self):
        lock.release(self.phone)

    def test_first_acquire_succeeds(self):
        self.assertTrue(lock.acquire(self.phone))

    def test_second_acquire_while_held_fails(self):
        self.assertTrue(lock.acquire(self.phone))
        self.assertFalse(lock.acquire(self.phone))

    def test_third_acquire_while_held_also_fails(self):
        # Mirrors the "triple click" row in the testing matrix: first tap
        # wins, everything else while the lock is held is turned away.
        self.assertTrue(lock.acquire(self.phone))
        self.assertFalse(lock.acquire(self.phone))
        self.assertFalse(lock.acquire(self.phone))

    def test_release_then_acquire_succeeds_again(self):
        self.assertTrue(lock.acquire(self.phone))
        lock.release(self.phone)
        self.assertTrue(lock.acquire(self.phone))

    def test_release_without_acquire_does_not_raise(self):
        # Must be safe to call from a `finally` block even on a path where
        # acquire() was never actually called or already returned False.
        lock.release(self.phone)  # no exception

    def test_release_is_idempotent(self):
        self.assertTrue(lock.acquire(self.phone))
        lock.release(self.phone)
        lock.release(self.phone)  # no exception, no error on double-release

    def test_empty_phone_always_acquires(self):
        # No phone to key on - never block, never need releasing.
        self.assertTrue(lock.acquire(""))
        self.assertTrue(lock.acquire(""))
        self.assertTrue(lock.acquire(None))

    def test_different_phones_do_not_block_each_other(self):
        other_phone = self.phone + "1"
        self.assertTrue(lock.acquire(self.phone))
        self.assertTrue(lock.acquire(other_phone))
        lock.release(other_phone)


class LockTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._counter = getattr(LockTimeoutTests, "_counter", 0) + 1
        LockTimeoutTests._counter = self._counter
        self.phone = f"91900001{self._counter:04d}"

    def tearDown(self):
        lock.release(self.phone)

    def test_lock_expires_after_timeout(self):
        # "Timeout after 5 sec -> Lock released" from the testing matrix,
        # sped up to a fraction of a second so the test suite stays fast.
        self.assertTrue(lock.acquire(self.phone, timeout_seconds=0.05))
        self.assertFalse(lock.acquire(self.phone, timeout_seconds=0.05))
        time.sleep(0.08)
        self.assertTrue(lock.acquire(self.phone, timeout_seconds=0.05))

    def test_default_timeout_is_within_guide_range(self):
        # Guide specifies a 2-5 second short-lived lock.
        self.assertGreaterEqual(lock.DEFAULT_TIMEOUT_SECONDS, 2.0)
        self.assertLessEqual(lock.DEFAULT_TIMEOUT_SECONDS, 5.0)


class LockConcurrencyTests(unittest.TestCase):
    """Simulates the actual double-tap race: two threads hitting acquire()
    for the same phone at effectively the same instant. Exactly one must
    win - this is the property the whole feature depends on."""

    def setUp(self):
        self.phone = "919000099999"

    def tearDown(self):
        lock.release(self.phone)

    def test_exactly_one_of_many_concurrent_acquires_wins(self):
        results = []
        results_guard = threading.Lock()
        start_barrier = threading.Barrier(10)

        def attempt():
            start_barrier.wait()  # line everyone up, then release together
            got = lock.acquire(self.phone, timeout_seconds=2.0)
            with results_guard:
                results.append(got)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 9)


class LockCleanupTests(unittest.TestCase):
    def test_cleanup_removes_only_expired_entries(self):
        expired_phone = "919000088881"
        live_phone = "919000088882"
        lock.acquire(expired_phone, timeout_seconds=0.02)
        lock.acquire(live_phone, timeout_seconds=5.0)
        time.sleep(0.05)

        removed = lock.cleanup()

        self.assertGreaterEqual(removed, 1)
        # The expired one is gone, so a fresh acquire on it must succeed...
        self.assertTrue(lock.acquire(expired_phone, timeout_seconds=1.0))
        # ...while the still-live one is untouched and stays held.
        self.assertFalse(lock.acquire(live_phone, timeout_seconds=1.0))

        lock.release(expired_phone)
        lock.release(live_phone)


if __name__ == "__main__":
    unittest.main()
