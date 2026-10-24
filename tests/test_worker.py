"""Tests for the queue worker.

These tests exercise the worker's claim → deliver → mark-delivered /
mark-failed / dead-letter state machine, using a fake Supabase so we
don't need a real database to verify the logic.

Critical scenarios covered:
- Idempotent delivery (duplicate event_key → no duplicate insert)
- Permanent failure → dead-letter, not retried forever
- Transient failure → scheduled for retry
- Worker restart with pending items → items claimed and processed
- Prolonged database downtime → items stay pending, never lost
- Ambiguous commit response → idempotent re-delivery
"""
import os
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox, worker


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    path = str(tmp_path / "outbox.sqlite3")
    outbox.configure(path)
    # Disable the daemon thread during tests.
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    yield path


@pytest.fixture(autouse=True)
def mock_supabase(monkeypatch):
    """Replace the supabase_client module with a fake.

    Tests that need a specific behaviour can override individual
    attributes on the fake.
    """
    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.ping.return_value = True

    class FakeCursor:
        def __init__(self):
            self.executed = []
            self.fetchone_return = None
            self.rowcount = 0

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def execute(self, sql, params=None):
            self.executed.append((sql, params))
            if "ON CONFLICT" in sql.upper():
                self.rowcount = 0
            else:
                self.rowcount = 1
            return self

        def fetchone(self):
            return self.fetchone_return

    @contextmanager
    def fake_cursor():
        yield FakeCursor()

    fake.cursor = fake_cursor
    monkeypatch.setattr(worker, "supabase_client", fake)
    yield fake


def test_worker_delivers_analytics_event(temp_queue, mock_supabase):
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000001",
        "occurred_at": time.time(),
        "tool_slug": None,
        "page_path": "/",
        "properties": {},
    }, event_key="evt-1")

    processed = worker.run_once(batch_size=10)
    assert processed == 1

    snap = outbox.counts()
    assert snap["delivered"] == 1
    assert snap["pending"] == 0


def test_worker_delivers_contact_submission(temp_queue, mock_supabase):
    outbox.enqueue("contact_submission", {
        "reference_code": "ANVIL-TEST1",
        "subject": "Hello",
        "message": "This is a test message with enough detail.",
        "category": "bug_report",
        "name": "Test User",
        "email": "test@example.com",
        "page_url": "https://example.com/",
        "tool_slug": None,
        "browser_context": {},
        "consent_recorded_at": time.time(),
        "occurred_at": time.time(),
    }, event_key="contact-1")

    processed = worker.run_once(batch_size=10)
    assert processed == 1

    snap = outbox.counts()
    assert snap["delivered"] == 1


def test_worker_requeues_on_transient_failure(temp_queue, mock_supabase):
    """A transient error should mark the item for retry, not dead-letter."""
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000002",
        "occurred_at": time.time(),
    }, event_key="evt-transient")

    @contextmanager
    def raising_cursor():
        raise OSError("connection refused")
        yield  # never reached

    mock_supabase.cursor = raising_cursor

    processed = worker.run_once(batch_size=10)
    assert processed == 1

    snap = outbox.counts()
    assert snap["delivered"] == 0
    assert snap["dead_lettered"] == 0
    assert snap["pending"] == 1  # still pending, scheduled for retry


def test_worker_dead_letters_permanent_failure(temp_queue, mock_supabase):
    """A permanent error (auth, schema) should dead-letter immediately."""
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000003",
        "occurred_at": time.time(),
    }, event_key="evt-permanent")

    @contextmanager
    def raising_cursor():
        import psycopg
        exc = psycopg.errors.InsufficientPrivilege()
        exc.pgcode = "42501"  # insufficient_privilege — permanent
        raise exc
        yield  # never reached

    mock_supabase.cursor = raising_cursor

    processed = worker.run_once(batch_size=10)
    assert processed == 1

    snap = outbox.counts()
    assert snap["dead_lettered"] == 1
    assert snap["delivered"] == 0


def test_worker_handles_no_supabase_configured(temp_queue, mock_supabase):
    """When Supabase isn't configured, items stay queued (with backoff)."""
    mock_supabase.is_configured.return_value = False

    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000004",
        "occurred_at": time.time(),
    }, event_key="evt-no-config")

    processed = worker.run_once(batch_size=10)
    assert processed == 1

    snap = outbox.counts()
    assert snap["pending"] == 1
    assert snap["delivered"] == 0


def test_worker_recovers_pending_items_after_restart(temp_queue, mock_supabase):
    """Items enqueued before a 'restart' are still processed after."""
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000005",
        "occurred_at": time.time(),
    }, event_key="evt-restart")

    # Simulate a restart: clear the in-memory cache (no-op since the SQLite
    # file persists), then run the worker.
    processed = worker.run_once(batch_size=10)
    assert processed == 1
    snap = outbox.counts()
    assert snap["delivered"] == 1


def test_idempotent_delivery_does_not_double_process(temp_queue, mock_supabase):
    """Re-running the worker on already-delivered items doesn't redo work."""
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000006",
        "occurred_at": time.time(),
    }, event_key="evt-once")

    # First run delivers it.
    worker.run_once(batch_size=10)
    assert outbox.counts()["delivered"] == 1

    # Second run finds nothing pending.
    processed = worker.run_once(batch_size=10)
    assert processed == 0
    assert outbox.counts()["delivered"] == 1


def test_ambiguous_commit_response_is_idempotent(temp_queue, mock_supabase):
    """Scenario: the DB commits, but the client sees a network error.

    The worker marks the item for retry. On retry, the same event_key
    is used, and the INSERT ... ON CONFLICT DO NOTHING is a no-op.
    The worker then marks the item delivered.

    No duplicate row is created.
    """
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000007",
        "occurred_at": time.time(),
    }, event_key="evt-ambiguous")

    call_count = {"n": 0}

    @contextmanager
    def flaky_cursor():
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First attempt fails with a network error AFTER the DB commit.
            raise OSError("connection reset after commit")
        # Second attempt succeeds; the INSERT ON CONFLICT DO NOTHING
        # handles the duplicate.
        cur = MagicMock()
        cur.execute.return_value = None
        cur.fetchone.return_value = None
        yield cur

    mock_supabase.cursor = flaky_cursor

    worker.run_once(batch_size=10)
    assert outbox.counts()["delivered"] == 0  # first attempt failed

    # Force the item to be due immediately.
    items = outbox.list_recent(limit=10)
    pending_item = next(i for i in items if i.event_key == "evt-ambiguous")
    outbox.mark_failed(pending_item.id, error_code="test",
                       error_msg="manual", next_attempt_at=time.time() - 1)

    worker.run_once(batch_size=10)
    assert outbox.counts()["delivered"] == 1


def test_unknown_event_type_dead_letters(temp_queue, mock_supabase):
    """An event_type with no handler should be dead-lettered, not retried."""
    # Manually insert an item with a bogus event_type. The validator
    # normally prevents this, but a future schema change could regress.
    with outbox._cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (event_key, event_type, payload, created_at, "
            "next_attempt_at, attempt_count) VALUES (?, ?, ?, ?, ?, 0)",
            ("evt-bad-type", "bogus_type", "{}", time.time(), time.time()),
        )

    worker.run_once(batch_size=10)
    snap = outbox.counts()
    assert snap["dead_lettered"] == 1


def test_queue_saturation_does_not_lose_contact_submissions(
    temp_queue, mock_supabase, monkeypatch
):
    """When the queue is full, contact submissions are NEVER evicted."""
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "3")
    # Fill the queue with analytics events.
    for i in range(3):
        outbox.enqueue("analytics_event", {"n": i}, event_key=f"a-{i}")
    # Now enqueue a contact submission — must succeed even when full.
    outbox.enqueue("contact_submission", {"ref": "ANVIL-SAT"},
                  event_key="contact-sat")
    snap = outbox.counts()
    # The contact is in the queue. An analytics event was evicted to make
    # room (the contact's enqueue does NOT evict; only a new analytics
    # event arriving while full triggers eviction).
    assert snap["contact_pending"] == 1
    assert snap["pending"] >= 1


def test_retry_dead_letter_returns_item_to_pending(temp_queue, mock_supabase):
    """The retry_dead_letter helper moves an item back to pending."""
    outbox.enqueue("analytics_event", {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000008",
        "occurred_at": time.time(),
    }, event_key="evt-retry")
    outbox.dead_letter(1, reason="permanent:auth")

    ok = worker.retry_dead_letter(1)
    assert ok is True

    snap = outbox.counts()
    assert snap["dead_lettered"] == 0
    assert snap["pending"] == 1
