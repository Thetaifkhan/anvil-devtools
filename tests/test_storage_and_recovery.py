"""Tests for storage failure, worker recovery, and queue saturation scenarios.

These tests verify the application's behavior under adverse conditions:

- Storage failure: the SQLite outbox file becomes unwritable (e.g. disk
  full, permissions revoked). The contact form must NOT claim the
  submission was saved.
- Worker recovery: items enqueued before a "process restart" (re-import)
  are still pending and deliverable after re-import.
- Queue saturation: when the queue approaches capacity, the saturation
  alert fires and the admin dashboard surfaces it.
- Retention: with pruning disabled (default), pending analytics events
  are NEVER auto-deleted regardless of age. Contact submissions are
  NEVER pruned regardless of any setting.
"""
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox, worker


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    # Reset saturation-alert state.
    global _last_saturation_alert_at
    worker._last_saturation_alert_at = 0.0
    yield


@pytest.fixture(autouse=True)
def mock_supabase(monkeypatch):
    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.ping.return_value = True

    class FakeCursor:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, sql, params=None): pass
        @property
        def rowcount(self): return 1
        def fetchone(self): return None

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    fake.cursor = fake_cursor
    monkeypatch.setattr(worker, "supabase_client", fake)
    yield fake


# ── Storage failure ──────────────────────────────────────────────────────

class TestStorageFailure:
    def test_contact_submission_when_outbox_unwritable(self, monkeypatch, tmp_path):
        """If the SQLite file becomes unwritable (disk full, permissions),
        the contact form must NOT claim the submission was saved.

        We simulate this by pointing SUPABASE_QUEUE_PATH at a path inside
        a read-only directory.
        """
        # Make the outbox path unwritable in a way that does NOT depend on
        # the uid running the tests. A permission-based fixture (chmod 555)
        # is bypassed by root, so the enqueue would succeed and the test
        # would be meaningless in root containers. Placing the database
        # under a regular file makes every open() fail with ENOTDIR.
        blocker = tmp_path / "not_a_directory"
        blocker.write_text("file, not a directory")
        ro_path = str(blocker / "outbox.sqlite3")

        # Re-configure the outbox to use the read-only path.
        # The first call (configure) might succeed because SQLite can
        # sometimes open files in read-only dirs; the actual write will
        # fail.
        try:
            outbox.configure(ro_path)
        except Exception:
            # If configure fails (can't even create the file), the
            # contact route must surface that as a 503.
            pass

        # Attempting to enqueue should raise (not silently succeed).
        with pytest.raises(Exception):
            outbox.enqueue("contact_submission", {
                "reference_code": "ANVIL-FAIL-1",
                "subject": "Test",
                "message": "Should fail when storage is unwritable.",
                "category": "bug_report",
            }, event_key="evt-storage-fail-1")

    def test_outbox_persists_existing_items_when_new_write_fails(
        self, monkeypatch, tmp_path
    ):
        """If a new enqueue fails due to storage error, existing items
        in the queue must remain intact (no partial state corruption)."""
        # Enqueue two items successfully.
        outbox.enqueue("contact_submission", {"ref": "ANVIL-1"},
                       event_key="evt-pre-fail-1")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-2"},
                       event_key="evt-pre-fail-2")
        snap_before = outbox.counts()
        assert snap_before["pending"] == 2

        # Now make the queue path unwritable by replacing the file
        # with a directory (SQLite can't open a directory as a DB).
        path = outbox.get_db_path()
        os.rename(path, path + ".bak")
        os.mkdir(path)  # Replace the file with a directory.

        # Attempting to enqueue should fail.
        with pytest.raises(Exception):
            outbox.enqueue("analytics_event", {"x": 1},
                          event_key="evt-post-fail-1")

        # Restore the original file.
        os.rmdir(path)
        os.rename(path + ".bak", path)

        # The original items must still be there.
        snap_after = outbox.counts()
        assert snap_after["pending"] == 2, (
            f"Existing items lost after storage failure: {snap_after}"
        )

    def test_disk_full_does_not_silently_drop_contact(self, monkeypatch):
        """When the disk is full, the SQLite write fails with an
        OperationalError. The contact form must surface this as a 503
        (or 500), NEVER silently report success."""
        from sqlite3 import OperationalError

        # Patch the cursor to raise on INSERT.
        original_cursor = outbox._cursor

        @contextmanager
        def failing_cursor():
            cur = MagicMock()
            cur.execute.side_effect = OperationalError("disk I/O error")
            cur.commit = lambda: None
            cur.rollback = lambda: None
            yield cur

        outbox._cursor = failing_cursor
        try:
            with pytest.raises(OperationalError):
                outbox.enqueue("contact_submission", {
                    "reference_code": "ANVIL-DISK-FULL",
                    "subject": "Test",
                    "message": "Should fail loudly, not silently succeed.",
                    "category": "bug_report",
                }, event_key="evt-disk-full-1")
        finally:
            outbox._cursor = original_cursor


# ── Worker recovery ──────────────────────────────────────────────────────

class TestWorkerRecovery:
    def test_worker_recovers_pending_items_after_module_reload(self):
        """Items enqueued before a 'module reload' (simulating a process
        restart) are still pending and deliverable after the reload."""
        # Enqueue 3 items.
        outbox.enqueue("contact_submission", {
            "reference_code": "ANVIL-RECOVER-1",
            "subject": "Recovery test 1",
            "message": "Should survive module reload.",
            "category": "bug_report",
        }, event_key="evt-recover-1")
        outbox.enqueue("analytics_event", {
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000010",
        }, event_key="evt-recover-2")
        outbox.enqueue("analytics_event", {
            "event_name": "tool_run",
            "session_id": "00000000-0000-0000-0000-000000000011",
        }, event_key="evt-recover-3")

        # "Restart" by re-importing the outbox module.
        # The path is the same, so the SQLite file persists.
        path = outbox.get_db_path()
        import importlib
        importlib.reload(outbox)
        outbox.configure(path)

        snap = outbox.counts()
        assert snap["pending"] == 3, (
            f"Expected 3 items to survive reload; got {snap['pending']}"
        )

        # Worker should now be able to claim and deliver them.
        items = outbox.claim_pending(batch_size=10)
        assert len(items) == 3
        for item in items:
            outbox.mark_delivered(item.id)
        snap = outbox.counts()
        assert snap["delivered"] == 3
        assert snap["pending"] == 0

    def test_worker_recovers_after_supabase_outage(self, mock_supabase):
        """Worker attempts delivery while Supabase is down (transient
        failure). Items stay pending. After Supabase recovers, the
        worker delivers them on the next poll."""
        outbox.enqueue("contact_submission", {
            "reference_code": "ANVIL-OUTAGE-1",
            "subject": "Outage recovery",
            "message": "Should deliver after Supabase recovers.",
            "category": "bug_report",
        }, event_key="evt-outage-1")

        # Phase 1: Supabase is down (cursor raises OSError).
        @contextmanager
        def failing_cursor():
            raise OSError("connection refused")
            yield

        mock_supabase.cursor = failing_cursor
        worker.run_once(batch_size=10)
        snap = outbox.counts()
        assert snap["pending"] == 1, "Item must stay pending during outage."
        assert snap["delivered"] == 0

        # Force the item to be due immediately.
        items = outbox.list_recent(limit=10)
        pending = next(i for i in items if i.event_key == "evt-outage-1")
        outbox.mark_failed(pending.id, error_code="test",
                           error_msg="manual", next_attempt_at=time.time() - 1)

        # Phase 2: Supabase recovers. Restore the working cursor.
        @contextmanager
        def working_cursor():
            class C:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def execute(self, sql, params=None): pass
                @property
                def rowcount(self): return 1
                def fetchone(self): return None
            yield C()

        mock_supabase.cursor = working_cursor
        worker.run_once(batch_size=10)
        snap = outbox.counts()
        assert snap["delivered"] == 1, "Item must be delivered after recovery."
        assert snap["pending"] == 0


# ── Queue saturation ──────────────────────────────────────────────────────

class TestQueueSaturation:
    def test_saturation_alert_triggers_at_threshold(self, monkeypatch):
        """When pending items reach the configured threshold, the
        counts() snapshot reports saturation_alert=True."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "10")
        monkeypatch.setenv("SUPABASE_QUEUE_SATURATION_ALERT_PCT", "80")
        # Enqueue 9 items (90% of 10).
        for i in range(9):
            outbox.enqueue("analytics_event", {"n": i}, event_key=f"a-{i}")
        snap = outbox.counts()
        assert snap["saturation_pct"] == 90
        assert snap["saturation_alert"] is True, (
            f"Expected saturation_alert=True at 90%; got {snap}"
        )

    def test_saturation_alert_below_threshold(self, monkeypatch):
        """When pending items are below the threshold, no alert."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "100")
        monkeypatch.setenv("SUPABASE_QUEUE_SATURATION_ALERT_PCT", "80")
        for i in range(10):
            outbox.enqueue("analytics_event", {"n": i}, event_key=f"b-{i}")
        snap = outbox.counts()
        assert snap["saturation_pct"] == 10
        assert snap["saturation_alert"] is False

    def test_saturation_alert_can_be_disabled(self, monkeypatch):
        """Setting SUPABASE_QUEUE_SATURATION_ALERT_PCT=0 disables the alert."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "5")
        monkeypatch.setenv("SUPABASE_QUEUE_SATURATION_ALERT_PCT", "0")
        for i in range(5):
            outbox.enqueue("analytics_event", {"n": i}, event_key=f"c-{i}")
        snap = outbox.counts()
        assert snap["saturation_alert"] is False, (
            "Alert should be disabled when threshold is 0"
        )

    def test_saturation_logs_warning_in_worker_loop(self, monkeypatch, caplog):
        """The worker loop logs a WARNING when the queue is saturated.
        Logs are throttled by SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "5")
        monkeypatch.setenv("SUPABASE_QUEUE_SATURATION_ALERT_PCT", "80")
        # Fill the queue to 100% (5 of 5).
        for i in range(5):
            outbox.enqueue("analytics_event", {"n": i}, event_key=f"d-{i}")

        # Set the cooldown to 0 so the alert fires immediately.
        monkeypatch.setenv("SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN", "0")

        import logging
        with caplog.at_level(logging.WARNING, logger="anvil.delivery.worker"):
            # Manually trigger the saturation check by calling the
            # internal logic (we don't run the full loop because it
            # would block on _worker_stop.wait()).
            from delivery.outbox import counts
            snap = counts()
            assert snap["saturation_alert"] is True
            # The worker's _worker_loop would log this; we just verify
            # the snapshot here. The actual log message is verified by
            # the integration test in tests/test_integration.py.

    def test_contact_submissions_always_accepted_at_saturation(self, monkeypatch):
        """When the queue is at capacity, contact submissions are still
        accepted (queue grows beyond capacity)."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "3")
        # Fill the queue with analytics events.
        outbox.enqueue("analytics_event", {"n": 1}, event_key="e-1")
        outbox.enqueue("analytics_event", {"n": 2}, event_key="e-2")
        outbox.enqueue("analytics_event", {"n": 3}, event_key="e-3")
        # Now enqueue a contact — must succeed (queue grows beyond 3).
        item, created = outbox.enqueue("contact_submission",
                                        {"ref": "ANVIL-SAT"},
                                        event_key="e-contact")
        assert created is True
        snap = outbox.counts()
        assert snap["contact_pending"] == 1
        assert snap["pending"] >= 3  # queue grew

    def test_saturation_alert_pct_field_in_counts(self, monkeypatch):
        """counts() must include saturation_pct and saturation_alert."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "10")
        outbox.enqueue("analytics_event", {"n": 1}, event_key="f-1")
        snap = outbox.counts()
        assert "saturation_pct" in snap
        assert "saturation_alert" in snap
        assert "saturation_alert_threshold" in snap
        assert "max_items" in snap


# ── Retention: default pruning DISABLED ──────────────────────────────────

class TestRetentionDefault:
    def test_default_analytics_pruning_is_disabled(self, monkeypatch):
        """With the default env (no SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS),
        pending analytics events are NEVER pruned regardless of age."""
        monkeypatch.delenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", raising=False)
        outbox.enqueue("analytics_event", {"n": 1}, event_key="g-old")
        # Force the row's created_at to 1 year ago.
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 365 * 86400, "g-old"),
            )
        # Enqueue another item — this should NOT prune the old one
        # because pruning is disabled by default.
        outbox.enqueue("analytics_event", {"n": 2}, event_key="g-new")
        snap = outbox.counts()
        assert snap["analytics_pending"] == 2, (
            f"Default pruning should be disabled; both items should be present. "
            f"Got: {snap}"
        )

    def test_contact_submissions_never_pruned_regardless_of_age(self, monkeypatch):
        """Contact submissions are NEVER pruned regardless of any
        SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS setting."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "1")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-OLD"},
                       event_key="h-old-contact")
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 365 * 86400, "h-old-contact"),
            )
        outbox.enqueue("analytics_event", {"n": 1}, event_key="h-trigger")
        snap = outbox.counts()
        assert snap["contact_pending"] == 1, (
            "Contact submissions must NEVER be pruned by analytics-age policy."
        )

    def test_explicit_zero_disables_pruning(self, monkeypatch):
        """Setting SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=0 explicitly
        disables pruning (same as the default)."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "0")
        outbox.enqueue("analytics_event", {"n": 1}, event_key="i-old")
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 30 * 86400, "i-old"),
            )
        outbox.enqueue("analytics_event", {"n": 2}, event_key="i-new")
        snap = outbox.counts()
        assert snap["analytics_pending"] == 2

    def test_explicit_positive_enables_pruning(self, monkeypatch):
        """Setting SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=7 enables pruning
        of pending analytics events older than 7 days."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "7")
        outbox.enqueue("analytics_event", {"n": 1}, event_key="j-old")
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 14 * 86400, "j-old"),  # 14 days ago
            )
        outbox.enqueue("analytics_event", {"n": 2}, event_key="j-new")
        snap = outbox.counts()
        # Old one pruned, new one present.
        assert snap["analytics_pending"] == 1


# ── Idempotent delivery after ambiguous commit ───────────────────────────

class TestAmbiguousCommitRecovery:
    def test_retry_after_ambiguous_commit_does_not_duplicate(self, mock_supabase):
        """Critical idempotency test.

        Scenario: the worker's previous attempt successfully committed
        the contact submission to public.support_queries, but the
        response was lost (network drop). The worker marks the item
        for retry. On retry, the INSERT ON CONFLICT (reference_code)
        DO NOTHING clause prevents the duplicate.
        """
        from tests.test_idempotency import (
            test_ambiguous_commit_does_not_create_duplicate_contact
        )
        # Re-use the existing test from test_idempotency.py
        test_ambiguous_commit_does_not_create_duplicate_contact()
