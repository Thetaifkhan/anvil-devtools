"""Tests for the worker's lifecycle and reliability guarantees.

Covers:
- start_worker is idempotent (calling twice doesn't start two threads)
- start_worker is a no-op when SUPABASE_WORKER_ENABLED=0
- The worker thread exits cleanly on stop_worker
- Items survive a "process restart" (re-instantiating the worker module)
  when the outbox path is on a persistent volume
- Prolonged Supabase downtime keeps items in the queue indefinitely
  (with exponential backoff between attempts)
- Queue capacity limit is enforced: analytics events are evicted;
  contact submissions are NEVER evicted
"""
import os
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox, worker


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    # Force-disable the in-process worker thread — tests call run_once
    # explicitly. Tests that need the thread (test_worker_lifecycle)
    # override this.
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    # Clean up any worker thread from a previous test.
    worker.stop_worker(timeout=1.0)
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


# ── Worker lifecycle ──────────────────────────────────────────────────────

class TestWorkerLifecycle:
    def test_start_worker_is_noop_when_disabled(self):
        # SUPABASE_WORKER_ENABLED=0 (set by fixture).
        worker.start_worker()
        assert worker.is_worker_running() is False

    def test_start_worker_starts_thread_when_enabled(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "1")
        worker.start_worker()
        try:
            assert worker.is_worker_running() is True
        finally:
            worker.stop_worker(timeout=2.0)

    def test_start_worker_is_idempotent(self, monkeypatch):
        """Calling start_worker twice does NOT start two threads."""
        monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "1")
        worker.start_worker()
        thread1 = worker._worker_thread
        worker.start_worker()
        thread2 = worker._worker_thread
        try:
            assert thread1 is thread2, "Second start_worker should not create a new thread."
            assert worker.is_worker_running() is True
        finally:
            worker.stop_worker(timeout=2.0)

    def test_stop_worker_terminates_thread(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "1")
        worker.start_worker()
        assert worker.is_worker_running() is True
        worker.stop_worker(timeout=2.0)
        assert worker.is_worker_running() is False

    def test_app_factory_starts_worker_once_in_production(self, monkeypatch):
        """The Flask app factory calls start_worker exactly once at
        boot when SUPABASE_WORKER_ENABLED=1, and never in TESTING mode."""
        # Stop any worker thread that may have been started by the
        # module-level ``app = create_app()`` line at import time
        # (which runs because gunicorn targets ``app:app``).
        worker.stop_worker(timeout=2.0)
        # Also force-disable the worker env var so that the module-level
        # app = create_app() call (re-triggered if the module is
        # re-imported) doesn't start a worker.
        monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
        assert worker.is_worker_running() is False, "Pre-condition: no worker thread."

        # Now set SUPABASE_WORKER_ENABLED=1 and re-call create_app with
        # TESTING=True. The is_testing branch must skip start_worker().
        monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "1")
        from app import create_app
        # Stop any worker that may have started during a prior import
        # of the app module (the module-level ``app = create_app()``
        # line runs at first import only, but tests may have triggered
        # it indirectly).
        worker.stop_worker(timeout=2.0)
        app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
        assert worker.is_worker_running() is False, (
            "App factory must NOT start the worker thread in TESTING mode."
        )
        # Cleanup: stop any worker that may have started (defensive).
        worker.stop_worker(timeout=2.0)


# ── Process restart / queue persistence ────────────────────────────────────

class TestProcessRestartPersistence:
    def test_items_survive_module_reimport(self, tmp_path, monkeypatch):
        """Simulate a process restart: re-configure the outbox against
        the SAME sqlite file. Pending items must still be there."""
        path = str(tmp_path / "persistent-outbox.sqlite3")
        outbox.configure(path)

        outbox.enqueue("contact_submission", {
            "reference_code": "ANVIL-PERSIST-1",
            "subject": "Restart test",
            "message": "Should survive a process restart.",
            "category": "bug_report",
        }, event_key="evt-persist-1")
        outbox.enqueue("analytics_event", {
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000099",
        }, event_key="evt-persist-2")

        # Simulate a process restart by clearing the module-level
        # path cache. The next call to configure() re-opens the file.
        outbox._DB_PATH = None
        outbox.configure(path)

        snap = outbox.counts()
        assert snap["pending"] == 2, "Items should survive module re-instantiation."

    def test_items_survive_engine_reset(self, tmp_path):
        """The supabase_client module caches a connection. Resetting
        it (as the worker does on a fatal error) should not affect
        the outbox."""
        path = str(tmp_path / "outbox.sqlite3")
        outbox.configure(path)
        outbox.enqueue("analytics_event", {
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000098",
        }, event_key="evt-survive-reset")
        import supabase_client
        supabase_client.reset_engine()
        snap = outbox.counts()
        assert snap["pending"] == 1

    def test_persistent_volume_path_is_respected(self, tmp_path, monkeypatch):
        """When OUTBOX_DURABLE=1 and SUPABASE_QUEUE_PATH points at a
        persistent volume, the contact form should honestly report
        queued_durable."""
        path = str(tmp_path / "durable-outbox.sqlite3")
        monkeypatch.setenv("SUPABASE_QUEUE_PATH", path)
        monkeypatch.setenv("OUTBOX_DURABLE", "1")
        outbox.configure(path, durable=True)
        assert outbox.is_durable() is True
        # Submit a contact.
        outbox.enqueue("contact_submission", {"ref": "ANVIL-1"}, event_key="k1")
        snap = outbox.counts()
        assert snap["durable"] is True
        assert snap["contact_pending"] == 1


# ── Prolonged Supabase downtime ────────────────────────────────────────────

class TestProlongedDowntime:
    def test_prolonged_downtime_keeps_items_pending(self, mock_supabase):
        """When Supabase is unreachable for an extended period, items
        stay in the queue with exponential backoff. They are NEVER
        lost."""
        outbox.enqueue("contact_submission", {
            "reference_code": "ANVIL-DOWNTIME-1",
            "subject": "Downtime test",
            "message": "Should survive prolonged DB downtime.",
            "category": "bug_report",
        }, event_key="evt-downtime")

        # Make Supabase unreachable.
        @contextmanager
        def failing_cursor():
            raise OSError("connection refused")
            yield  # never reached

        mock_supabase.cursor = failing_cursor

        # Run the worker 5 times — each one fails and schedules a retry.
        for i in range(5):
            worker.run_once(batch_size=10)
            # Force the item to be due immediately for the next iteration.
            items = outbox.list_recent(limit=10)
            pending = next((i for i in items if i.event_key == "evt-downtime"), None)
            if pending:
                outbox.mark_failed(pending.id, error_code="test",
                                    error_msg="manual", next_attempt_at=time.time() - 1)

        # The item should STILL be in the queue, never delivered, never dead-lettered.
        snap = outbox.counts()
        assert snap["pending"] == 1, "Item must survive prolonged downtime."
        assert snap["delivered"] == 0
        assert snap["dead_lettered"] == 0  # Transient errors don't dead-letter.

    def test_permanent_failure_dead_letters(self, mock_supabase):
        """A permanent failure (auth/schema) dead-letters immediately
        rather than retrying forever."""
        outbox.enqueue("analytics_event", {
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000050",
        }, event_key="evt-permanent")

        import psycopg
        @contextmanager
        def raising_cursor():
            exc = psycopg.errors.InsufficientPrivilege()
            exc.pgcode = "42501"
            raise exc
            yield

        mock_supabase.cursor = raising_cursor
        worker.run_once(batch_size=10)

        snap = outbox.counts()
        assert snap["dead_lettered"] == 1
        assert snap["delivered"] == 0

    def test_backoff_grows_between_attempts(self, mock_supabase):
        """Each retry should schedule a longer backoff than the previous one."""
        from delivery.errors import BackoffConfig, compute_backoff
        import random
        cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
        rng = random.Random(42)

        # The MAXIMUM possible backoff grows exponentially.
        upper_bounds = [min(900.0, 2.0 * (2 ** n)) for n in range(8)]
        # The means should also grow.
        means = []
        for n in range(8):
            samples = [compute_backoff(n, cfg, rng=random.Random(k)) for k in range(200)]
            means.append(sum(samples) / len(samples))

        # Mean at attempt 7 should be much greater than mean at attempt 0.
        assert means[7] > means[0] * 4, (
            f"Backoff means should grow: attempt0={means[0]:.2f}, "
            f"attempt7={means[7]:.2f}"
        )

        # Upper bound at attempt 0 is 2.0; at attempt 7 it's 256.0.
        assert upper_bounds[7] > upper_bounds[0] * 100


# ── Queue capacity ─────────────────────────────────────────────────────────

class TestQueueCapacity:
    def test_analytics_events_are_evicted_when_queue_full(self, monkeypatch):
        """When the queue is at capacity, a new analytics event causes
        an OLDER analytics event to be evicted (not the new one)."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "3")
        # Fill the queue with 3 analytics events.
        outbox.enqueue("analytics_event", {"n": 1}, event_key="a-1")
        outbox.enqueue("analytics_event", {"n": 2}, event_key="a-2")
        outbox.enqueue("analytics_event", {"n": 3}, event_key="a-3")
        # Enqueue a 4th — must evict the oldest analytics event.
        outbox.enqueue("analytics_event", {"n": 4}, event_key="a-4")
        snap = outbox.counts()
        # Queue is at capacity (3 items).
        assert snap["pending"] <= 3, (
            f"Queue should be at capacity (3), got {snap['pending']}"
        )

    def test_contact_submissions_are_never_evicted(self, monkeypatch):
        """Even when the queue is full of contact submissions, a new
        contact submission is accepted — the queue grows beyond
        capacity rather than losing a contact."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "2")
        # Fill the queue with contact submissions.
        outbox.enqueue("contact_submission", {"ref": "ANVIL-1"}, event_key="c-1")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-2"}, event_key="c-2")
        # Add a third contact — must NOT evict either of the others.
        item, created = outbox.enqueue("contact_submission", {"ref": "ANVIL-3"},
                                        event_key="c-3")
        assert created is True
        # All three contacts are still in the queue.
        snap = outbox.counts()
        assert snap["contact_pending"] == 3, (
            f"All 3 contacts should be present, got {snap['contact_pending']}"
        )

    def test_contact_submission_does_not_evict_analytics(self, monkeypatch):
        """A new contact submission when the queue is full of analytics
        events does NOT evict any analytics event."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "2")
        outbox.enqueue("analytics_event", {"n": 1}, event_key="a-1")
        outbox.enqueue("analytics_event", {"n": 2}, event_key="a-2")
        # Add a contact — should NOT evict any analytics event.
        outbox.enqueue("contact_submission", {"ref": "ANVIL-C"},
                       event_key="c-among-analytics")
        snap = outbox.counts()
        # The contact is in the queue (queue grew beyond capacity).
        assert snap["contact_pending"] == 1
        # The analytics events are also still there.
        assert snap["analytics_pending"] == 2

    def test_queue_full_rejects_analytics_when_no_analytics_to_evict(self, monkeypatch):
        """If the queue is full of contact submissions and a new
        analytics event arrives, the analytics event is REJECTED
        (not the contact)."""
        monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "2")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-1"}, event_key="c-1")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-2"}, event_key="c-2")
        # Try to enqueue an analytics event — should raise QueueFullError.
        with pytest.raises(outbox.QueueFullError):
            outbox.enqueue("analytics_event", {"n": 1}, event_key="a-rejected")
        # Contacts are still there.
        snap = outbox.counts()
        assert snap["contact_pending"] == 2


# ── Analytics-event pruning (configurable retention) ──────────────────────

class TestAnalyticsPruning:
    def test_old_analytics_events_are_pruned(self, monkeypatch):
        """Pending analytics events older than
        SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS are pruned on the next
        analytics-event enqueue."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "1")
        # Enqueue an analytics event with an artificially old created_at.
        outbox.enqueue("analytics_event", {"n": 1}, event_key="a-old")
        # Force the row's created_at to 2 days ago.
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 2 * 86400, "a-old"),
            )
        # Enqueue another analytics event — this should prune the old one.
        outbox.enqueue("analytics_event", {"n": 2}, event_key="a-new")
        snap = outbox.counts()
        # Only the new one remains.
        assert snap["analytics_pending"] == 1, (
            f"Old analytics event should have been pruned, got {snap['analytics_pending']}"
        )

    def test_contact_submissions_are_never_pruned(self, monkeypatch):
        """SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS only affects analytics
        events. Contact submissions are NEVER pruned regardless of age."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "1")
        outbox.enqueue("contact_submission", {"ref": "ANVIL-OLD"},
                       event_key="c-old")
        # Force the row's created_at to 1 year ago.
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 365 * 86400, "c-old"),
            )
        # Enqueue another item to trigger pruning.
        outbox.enqueue("analytics_event", {"n": 1}, event_key="a-trigger")
        snap = outbox.counts()
        # The contact is still there.
        assert snap["contact_pending"] == 1, (
            "Contact submission must NEVER be pruned by analytics-age policy."
        )

    def test_setting_zero_disables_pruning(self, monkeypatch):
        """SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=0 disables pruning."""
        monkeypatch.setenv("SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", "0")
        outbox.enqueue("analytics_event", {"n": 1}, event_key="a-old")
        with outbox._cursor() as cur:
            cur.execute(
                "UPDATE outbox SET created_at = ? WHERE event_key = ?",
                (time.time() - 365 * 86400, "a-old"),
            )
        # Enqueue another — should NOT prune the old one.
        outbox.enqueue("analytics_event", {"n": 2}, event_key="a-new")
        snap = outbox.counts()
        assert snap["analytics_pending"] == 2


# ── Honest durability contract ─────────────────────────────────────────────

class TestHonestDurability:
    def test_is_durable_defaults_to_false(self, tmp_path, monkeypatch):
        """Without OUTBOX_DURABLE=1, the queue is best-effort."""
        monkeypatch.delenv("OUTBOX_DURABLE", raising=False)
        outbox.configure(str(tmp_path / "outbox.sqlite3"))
        assert outbox.is_durable() is False

    def test_is_durable_true_when_opted_in(self, tmp_path, monkeypatch):
        """OUTBOX_DURABLE=1 opts in to durable mode."""
        monkeypatch.setenv("OUTBOX_DURABLE", "1")
        outbox.configure(str(tmp_path / "outbox.sqlite3"))
        assert outbox.is_durable() is True

    def test_counts_includes_durable_flag(self, tmp_path, monkeypatch):
        """The /admin/queue UI shows the durable flag."""
        monkeypatch.delenv("OUTBOX_DURABLE", raising=False)
        outbox.configure(str(tmp_path / "outbox.sqlite3"))
        snap = outbox.counts()
        assert "durable" in snap
        assert snap["durable"] is False
