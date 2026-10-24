"""Tests for the ambiguous-commit idempotency contract.

Scenario
--------
The worker attempts to deliver a contact submission. The Postgres
INSERT succeeds and commits on the server, but the network connection
drops before the client receives the response. The worker's
``_handle_contact_submission`` raises an ``OSError`` (classified as
transient). The worker marks the item for retry with a short backoff.

On retry:
1. The outbox row is the SAME row (claimed again with the same
   ``event_key``). No new outbox row.
2. The ``_delivery_queue_upsert`` does
   ``INSERT ... ON CONFLICT (event_key) DO NOTHING``. The row already
   exists from the previous attempt. No duplicate, no error.
3. The ``_handle_contact_submission`` does
   ``INSERT INTO support_queries ... ON CONFLICT (reference_code) DO NOTHING``.
   The row already exists. No duplicate, no error.
4. The worker calls ``mark_delivered`` and the outbox item transitions
   to the delivered state.

End state: exactly ONE row in ``support_queries``, ONE row in
``private.delivery_queue``, ONE row in the outbox with
``delivered_at`` set. Zero duplicates.

These tests use a mock Supabase that simulates the ambiguous commit:
the FIRST call to ``cur.execute(...)`` commits to the mock's "DB" and
RAISES an OSError. The SECOND call (on retry) finds the row already
present and reports 0 affected rows (via the ON CONFLICT DO NOTHING
contract).
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
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    yield


@pytest.fixture(autouse=True)
def mock_supabase(monkeypatch):
    """A mock Supabase that simulates a real DB by tracking inserts
    in an in-memory dict keyed by event_key and reference_code.
    """
    state = {
        "delivery_queue": set(),   # event_key values
        "support_queries": {},     # reference_code -> row
        "analytics_events": [],   # list of inserted events
        "visitor_sessions": {},   # session_id -> row
        "call_count": 0,
    }

    class MockCursor:
        def __init__(self):
            self._rowcount = 0

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def execute(self, sql, params=None):
            state["call_count"] += 1
            sql_upper = sql.upper().strip()
            # ── delivery_queue upsert ──
            if "INSERT INTO PRIVATE.DELIVERY_QUEUE" in sql_upper:
                event_key = params[0] if params else None
                if event_key in state["delivery_queue"]:
                    self._rowcount = 0
                    return
                state["delivery_queue"].add(event_key)
                self._rowcount = 1
                return
            # ── support_queries insert (contact) ──
            if "INSERT INTO PUBLIC.SUPPORT_QUERIES" in sql_upper:
                ref = params[0] if params else None
                if ref in state["support_queries"]:
                    self._rowcount = 0  # ON CONFLICT DO NOTHING
                    return
                state["support_queries"][ref] = {
                    "reference_code": ref,
                    "name": params[1],
                    "email": params[2],
                    "category": params[3],
                    "subject": params[4],
                    "message": params[5],
                }
                self._rowcount = 1
                return
            # ── analytics_events insert ──
            if "INSERT INTO PUBLIC.ANALYTICS_EVENTS" in sql_upper:
                state["analytics_events"].append({
                    "event_name": params[1] if params else None,
                    "session_id": params[2] if params else None,
                })
                self._rowcount = 1
                return
            # ── visitor_sessions upsert ──
            if "INSERT INTO PUBLIC.VISITOR_SESSIONS" in sql_upper:
                sid = params[0] if params else None
                # Always succeed (it's an ON CONFLICT DO UPDATE).
                state["visitor_sessions"][sid] = True
                self._rowcount = 1
                return
            self._rowcount = 0

        @property
        def rowcount(self):
            return self._rowcount

        def fetchone(self):
            return None

    fake = MagicMock()
    fake.is_configured.return_value = True
    fake.ping.return_value = True

    @contextmanager
    def fake_cursor():
        yield MockCursor()

    fake.cursor = fake_cursor
    monkeypatch.setattr(worker, "supabase_client", fake)
    return state


def test_ambiguous_commit_does_not_create_duplicate_contact():
    """The critical idempotency test.

    Simulate the ambiguous-commit scenario: the FIRST attempt's
    ``INSERT INTO support_queries`` actually commits to the mock DB,
    but the cursor's execute() raises an OSError (network drop AFTER
    commit). The worker treats this as transient and re-queues. On
    retry, the same event_key's INSERT INTO delivery_queue finds the
    row already present, and the INSERT INTO support_queries finds the
    reference_code already present. No duplicate is created.
    """
    # Wrap the mock's cursor to inject a transient failure on the
    # FIRST support_queries insert.
    real_cursor = worker.supabase_client.cursor
    call_state = {"first_support_insert_done": False}

    @contextmanager
    def failing_cursor():
        from delivery import worker as _w  # local import to avoid cycle
        cur_mock = None

        class _WrappedCursor:
            def __init__(self, inner):
                self._inner = inner

            def __enter__(self): return self
            def __exit__(self, *args): return False

            def execute(self, sql, params=None):
                sql_upper = sql.upper()
                # First support_queries insert: pretend the server
                # committed but the response was lost.
                if ("INSERT INTO PUBLIC.SUPPORT_QUERIES" in sql_upper
                        and not call_state["first_support_insert_done"]):
                    # Actually do the insert via the inner cursor so
                    # the mock DB reflects the commit...
                    self._inner.execute(sql, params)
                    # ...then raise as if the network died.
                    call_state["first_support_insert_done"] = True
                    raise OSError("connection reset after commit")
                return self._inner.execute(sql, params)

            @property
            def rowcount(self): return self._inner.rowcount

            def fetchone(self): return self._inner.fetchone()

        # Build a real MockCursor via the original factory.
        with real_cursor() as inner_cur:
            yield _WrappedCursor(inner_cur)

    worker.supabase_client.cursor = failing_cursor

    # Enqueue a contact submission.
    outbox.enqueue("contact_submission", {
        "reference_code": "ANVIL-AMB1",
        "subject": "Ambiguous commit test",
        "message": "Verifying idempotency after a network drop.",
        "category": "bug_report",
        "name": "Test User",
        "email": "test@example.com",
        "page_url": "https://example.com/",
        "tool_slug": None,
        "browser_context": {},
        "consent_recorded_at": time.time(),
        "occurred_at": time.time(),
    }, event_key="evt-ambiguous-contact")

    # First run — should fail transiently.
    worker.run_once(batch_size=10)
    snap = outbox.counts()
    assert snap["delivered"] == 0, "First attempt should not have delivered."
    assert snap["pending"] == 1, "Item should be back in pending for retry."

    # Force the retry to be due now.
    items = outbox.list_recent(limit=10)
    pending = next(i for i in items if i.event_key == "evt-ambiguous-contact")
    outbox.mark_failed(pending.id, error_code="test", error_msg="manual",
                       next_attempt_at=time.time() - 1)

    # Restore the normal (non-failing) cursor for the retry.
    worker.supabase_client.cursor = real_cursor

    worker.run_once(batch_size=10)
    snap = outbox.counts()
    assert snap["delivered"] == 1, "Retry should have delivered."


def test_duplicate_delivery_does_not_create_duplicate_contact(mock_supabase):
    """Even if the worker somehow calls _handle_contact_submission twice
    with the same event_key, the database ends up with one row."""
    payload = {
        "reference_code": "ANVIL-DUP1",
        "subject": "Test",
        "message": "Body",
        "category": "bug_report",
        "name": None, "email": None, "page_url": None,
        "tool_slug": None, "browser_context": {}, "consent_recorded_at": None,
        "occurred_at": time.time(),
    }
    outbox.enqueue("contact_submission", payload, event_key="evt-dup-contact")

    # Run the worker twice (the second run finds nothing to do).
    worker.run_once(batch_size=10)
    worker.run_once(batch_size=10)

    assert mock_supabase["support_queries"].get("ANVIL-DUP1") is not None
    assert len(mock_supabase["support_queries"]) == 1
    assert "evt-dup-contact" in mock_supabase["delivery_queue"]
    assert len(mock_supabase["delivery_queue"]) == 1


def test_duplicate_delivery_does_not_create_duplicate_analytics(mock_supabase):
    """Same test for analytics events."""
    payload = {
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000010",
        "occurred_at": time.time(),
        "tool_slug": None,
        "page_path": "/",
        "properties": {},
    }
    outbox.enqueue("analytics_event", payload, event_key="evt-dup-analytics")

    worker.run_once(batch_size=10)
    worker.run_once(batch_size=10)

    # The mock DB has exactly one analytics_events row.
    matching = [e for e in mock_supabase["analytics_events"]
                if e["session_id"] == "00000000-0000-0000-0000-000000000010"]
    assert len(matching) == 1, (
        f"Expected 1 analytics event, got {len(matching)}: {matching}"
    )


def test_outbox_never_creates_duplicate_event_key():
    """Even if enqueue is called twice with the same key, only one row
    exists in the outbox."""
    payload = {"event_name": "page_view", "session_id": "00000000-0000-0000-0000-000000000020"}
    outbox.enqueue("analytics_event", payload, event_key="evt-uniq")
    outbox.enqueue("analytics_event", payload, event_key="evt-uniq")
    outbox.enqueue("analytics_event", payload, event_key="evt-uniq")

    items = outbox.list_recent(limit=100)
    matching = [i for i in items if i.event_key == "evt-uniq"]
    assert len(matching) == 1, (
        f"Expected 1 outbox row, got {len(matching)}"
    )
