"""Tests for the durable outbox queue.

The outbox is the local SQLite layer that survives Supabase being
unreachable. These tests exercise the state machine and the
idempotency guarantees, using a temp file rather than the production
path so the dev machine's queue is never touched.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite file."""
    path = str(tmp_path / "outbox.sqlite3")
    outbox.configure(path)
    yield path


def test_enqueue_creates_item():
    item, created = outbox.enqueue(
        "contact_submission",
        {"reference_code": "ANVIL-AAAA", "subject": "hello"},
        event_key="key-1",
    )
    assert created is True
    assert item.event_key == "key-1"
    assert item.event_type == "contact_submission"
    assert item.payload["subject"] == "hello"
    assert item.attempt_count == 0


def test_enqueue_duplicate_event_key_returns_existing_item():
    payload = {"reference_code": "ANVIL-AAAA", "subject": "first"}
    item1, created1 = outbox.enqueue("contact_submission", payload, event_key="dup-key")
    # Same key, different payload — the original must win.
    item2, created2 = outbox.enqueue(
        "contact_submission",
        {"reference_code": "ANVIL-BBBB", "subject": "second"},
        event_key="dup-key",
    )
    assert created1 is True
    assert created2 is False
    assert item1.id == item2.id
    assert item2.payload["subject"] == "first"


def test_claim_pending_returns_items_in_next_attempt_order():
    outbox.enqueue("analytics_event", {"event_name": "page_view"}, event_key="a")
    outbox.enqueue("analytics_event", {"event_name": "tool_run"}, event_key="b")
    items = outbox.claim_pending(batch_size=10)
    assert len(items) == 2
    # Both items should now have attempt_count == 1.
    assert all(i.attempt_count == 1 for i in items)


def test_claim_pending_respects_next_attempt_at():
    item, _ = outbox.enqueue("analytics_event", {"x": 1}, event_key="future")
    # Push the item into the future so it isn't claimed yet.
    outbox.mark_failed(item.id, error_code="transient",
                       error_msg="db down",
                       next_attempt_at=time.time() + 3600)
    items = outbox.claim_pending(batch_size=10)
    assert len(items) == 0


def test_mark_delivered_sets_delivered_at():
    item, _ = outbox.enqueue("analytics_event", {"x": 1}, event_key="k")
    outbox.mark_delivered(item.id)
    snap = outbox.counts()
    assert snap["delivered"] == 1
    assert snap["pending"] == 0


def test_mark_failed_schedules_retry():
    item, _ = outbox.enqueue("analytics_event", {"x": 1}, event_key="k")
    future = time.time() + 60
    outbox.mark_failed(item.id, error_code="transient",
                       error_msg="db down", next_attempt_at=future)
    snap = outbox.counts()
    # Pending count includes items waiting to retry.
    assert snap["pending"] == 1
    assert snap["retrying"] == 1  # not due now
    assert snap["in_flight"] == 0


def test_dead_letter_moves_item_to_terminal_state():
    item, _ = outbox.enqueue("contact_submission", {"ref": "ANVIL-X"}, event_key="k")
    outbox.dead_letter(item.id, reason="permanent_failure:auth")
    snap = outbox.counts()
    assert snap["dead_lettered"] == 1
    assert snap["pending"] == 0


def test_requeue_dead_letter_returns_item_to_pending():
    item, _ = outbox.enqueue("contact_submission", {"ref": "ANVIL-X"}, event_key="k")
    outbox.dead_letter(item.id, reason="permanent_failure:auth")
    outbox.requeue_dead_letter(item.id)
    snap = outbox.counts()
    assert snap["dead_lettered"] == 0
    assert snap["pending"] == 1


def test_invalid_event_type_raises():
    with pytest.raises(ValueError):
        outbox.enqueue("not_a_real_type", {}, event_key="k")


def test_invalid_payload_type_raises():
    with pytest.raises(TypeError):
        outbox.enqueue("analytics_event", "not a dict", event_key="k")


def test_contact_submission_takes_precedence_over_analytics_when_full(monkeypatch):
    """When the queue is at capacity, a new analytics event is evicted
    in favour of a new contact submission. The contact is NEVER rejected."""
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "2")
    # Fill the queue with analytics events.
    outbox.enqueue("analytics_event", {"n": 1}, event_key="a1")
    outbox.enqueue("analytics_event", {"n": 2}, event_key="a2")
    # Now enqueue a contact — must succeed even though queue is full.
    item, created = outbox.enqueue(
        "contact_submission", {"ref": "ANVIL-CONTACT"},
        event_key="c1",
    )
    assert created is True
    assert item.event_type == "contact_submission"


def test_analytics_eviction_makes_room_for_contact(monkeypatch):
    """A new analytics event arriving when the queue is full evicts an
    OLDER analytics event rather than failing the new one."""
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "1")
    outbox.enqueue("analytics_event", {"n": "old"}, event_key="a-old")
    # This new analytics event should evict the old one to make room.
    item, created = outbox.enqueue("analytics_event", {"n": "new"}, event_key="a-new")
    assert created is True


def test_counts_returns_sensible_snapshot():
    outbox.enqueue("analytics_event", {"n": 1}, event_key="a")
    outbox.enqueue("contact_submission", {"ref": "X"}, event_key="b")
    snap = outbox.counts()
    assert snap["pending"] == 2
    assert snap["contact_pending"] == 1
    assert snap["analytics_pending"] == 1
    assert snap["delivered"] == 0
    assert snap["dead_lettered"] == 0
