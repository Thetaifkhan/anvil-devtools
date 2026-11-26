"""Phase 2 §2.3 outbox/worker hardening — tests.

Covers: the single-writer file lock (second live process fails fast with a
clear message; claims stay multi-process safe), the daily integrity check
with corrupt-file recovery, the explicit timestamp schema map (including
the "new field without conversion" guard), clock-skew clamping, and
readable dead-letter reasons with the full error kept separately.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox
from delivery import worker as worker_mod


@pytest.fixture(autouse=True)
def clean_env(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    monkeypatch.delenv("OUTBOX_ALLOW_MULTI_PROCESS", raising=False)
    yield


# ── 1. Single-writer lock ──────────────────────────────────────────────────

def test_second_live_writer_fails_fast_with_clear_message(tmp_path, caplog):
    path = str(tmp_path / "shared.sqlite3")

    # "Process A" holds the writer lock.
    import fcntl
    holder_fd = os.open(path + ".writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(path + ".writer.lock", "w") as fh:
        fh.write(str(os.getpid()))  # a live PID (this test process)

    # "Process B" (simulated: fresh module state, same path) configures.
    prev_fd, outbox._WRITER_LOCK_FD = outbox._WRITER_LOCK_FD, None
    outbox._WRITER_CONFLICT = False
    try:
        with caplog.at_level("ERROR"):
            outbox.configure(path)
        assert outbox._WRITER_CONFLICT is True
        assert any("already owns the outbox" in rec.getMessage()
                   for rec in caplog.records), (
            "the log line must clearly name the conflict")

        # Enqueue must fail fast with the clear message (and the contact
        # route maps sqlite3.OperationalError to a friendly 503).
        with pytest.raises(outbox.OutboxWriterConflictError,
                           match="already owns the outbox"):
            outbox.enqueue("analytics_event", {"x": 1}, event_key="w-1")
    finally:
        outbox._WRITER_CONFLICT = False
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
        if prev_fd is not None:
            outbox._WRITER_LOCK_FD = prev_fd


def test_claims_remain_multi_process_safe_with_lock(tmp_path):
    """The writer lock must not interfere with claiming: two 'processes'
    (fresh module states) can still claim disjointly."""
    path = str(tmp_path / "claims.sqlite3")
    outbox.configure(path)
    for i in range(6):
        outbox.enqueue("analytics_event", {"i": i}, event_key=f"c-{i}")

    # Simulate a second claimer: reset the conflict flag only (a real
    # second process would hold no writer lock and only claim).
    outbox._WRITER_CONFLICT = False
    a = outbox.claim_pending(batch_size=3)
    b = outbox.claim_pending(batch_size=3)
    keys = {i.event_key for i in a} | {i.event_key for i in b}
    assert keys == {f"c-{i}" for i in range(6)}
    assert not (a and b and {i.id for i in a} & {i.id for i in b})


def test_multi_process_opt_in_allows_writing(tmp_path, monkeypatch, caplog):
    path = str(tmp_path / "optin.sqlite3")
    import fcntl
    holder_fd = os.open(path + ".writer.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    with open(path + ".writer.lock", "w") as fh:
        fh.write(str(os.getpid()))

    monkeypatch.setenv("OUTBOX_ALLOW_MULTI_PROCESS", "1")
    outbox._WRITER_LOCK_FD = None
    outbox._WRITER_CONFLICT = False
    outbox._MULTI_PROCESS_WARNED = False
    try:
        with caplog.at_level("WARNING"):
            outbox.configure(path)
        item, created = outbox.enqueue("analytics_event", {"x": 1},
                                       event_key="optin-1")
        assert created is True
        assert any("accepted the risk" in rec.getMessage()
                   for rec in caplog.records)
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)


def test_dead_lock_owner_is_taken_over(tmp_path):
    path = str(tmp_path / "dead.sqlite3")
    # A stale lock file pointing at a PID that cannot exist.
    with open(path + ".writer.lock", "w") as fh:
        fh.write("999999999")
    outbox._WRITER_LOCK_FD = None
    outbox._WRITER_CONFLICT = False
    outbox.configure(path)  # must take over the dead owner's lock
    assert outbox._WRITER_CONFLICT is False
    item, created = outbox.enqueue("analytics_event", {"x": 1}, event_key="d-1")
    assert created is True


# ── 2. Daily integrity check ───────────────────────────────────────────────

def test_integrity_check_passes_on_healthy_db(tmp_path, caplog):
    path = str(tmp_path / "ok.sqlite3")
    outbox.configure(path)
    outbox._last_integrity_check_at = 0.0  # force the check
    with caplog.at_level("INFO"):
        outbox._integrity_check_if_due(path)
    assert any("integrity check passed" in rec.getMessage()
               for rec in caplog.records)


def test_integrity_check_moves_corrupt_file_aside_and_starts_fresh(
        tmp_path, caplog):
    path = str(tmp_path / "bad.sqlite3")
    outbox.configure(path)
    outbox.enqueue("contact_submission", {"ref": "R"}, event_key="bad-1")

    # Corrupt the file, then force the (daily) check.
    with open(path, "r+b") as fh:
        fh.write(b"CORRUPTED HEADER" * 8)
    outbox._last_integrity_check_at = 0.0
    with caplog.at_level("ERROR"):
        outbox._integrity_check_if_due(path)

    assert any("integrity check FAILED" in rec.getMessage()
               for rec in caplog.records)
    # The broken file was moved aside, not deleted.
    backups = [f for f in os.listdir(str(tmp_path))
               if f.startswith("bad.sqlite3.corrupt-")]
    assert backups, "corrupt file must be preserved for manual review"
    # A fresh outbox exists and accepts writes again.
    assert outbox.enqueue("contact_submission", {"ref": "R2"},
                          event_key="bad-2")[1] is True
    snap = outbox.counts()
    assert snap["pending"] == 1  # only the new item; old queue is in backup


def test_integrity_check_runs_at_most_once_per_day(tmp_path, caplog):
    path = str(tmp_path / "once.sqlite3")
    outbox.configure(path)
    outbox._last_integrity_check_at = 0.0

    # The first enqueue triggers the check (INFO log + timestamp stamp).
    with caplog.at_level("INFO", logger="anvil.delivery.outbox"):
        outbox.enqueue("analytics_event", {"x": 1}, event_key="i-1")
        outbox.enqueue("analytics_event", {"x": 2}, event_key="i-2")
        outbox.enqueue("analytics_event", {"x": 3}, event_key="i-3")
    stamp_after_first = outbox._last_integrity_check_at
    assert stamp_after_first > 0, "the due check must have run once"
    runs = [r for r in caplog.records
            if "integrity check" in r.getMessage()]
    assert len(runs) == 1, (
        f"quick_check ran {len(runs)} times for 3 enqueues; it must run at "
        "most once per day (the per-enqueue call must early-return)")


# ── 3. Timestamp schema map ────────────────────────────────────────────────

def test_schema_map_covers_every_at_field_per_event_type():
    """Guard: a new ``*_at`` field added to KNOWN_FIELDS without being
    registered in TIMESTAMP_FIELDS fails here (it would reach Postgres as a
    raw epoch instead of a datetime)."""
    for event_type, fields in worker_mod.KNOWN_FIELDS.items():
        at_fields = {f for f in fields if f.endswith("_at")}
        missing = at_fields - worker_mod.TIMESTAMP_FIELDS.get(event_type, set())
        assert not missing, (
            f"{event_type}: timestamp-looking fields not in TIMESTAMP_FIELDS: "
            f"{sorted(missing)}. Register them so the worker converts them.")


def test_conversion_clamps_and_passes_through():
    now = time.time()
    # Analytics event: occurred_at in the future -> clamped.
    p = worker_mod._convert_timestamps(
        "analytics_event", {"occurred_at": now + 999999, "event_name": "x"})
    assert p["occurred_at"] <= now + worker_mod.MAX_FUTURE_SKEW_SECONDS

    # Far past -> clamped.
    p = worker_mod._convert_timestamps(
        "contact_submission", {"occurred_at": 100})
    assert p["occurred_at"] > now - worker_mod.MAX_AGE_SECONDS - 1

    # Sane values pass through untouched.
    sane = now - 60
    p = worker_mod._convert_timestamps(
        "contact_submission", {"occurred_at": sane})
    assert p["occurred_at"] == sane

    # Non-timestamp fields untouched.
    p = worker_mod._convert_timestamps(
        "contact_submission", {"subject": "hello", "occurred_at": sane})
    assert p["subject"] == "hello"


def test_conversion_covers_session_and_audit_types():
    now = time.time()
    sess = worker_mod._convert_timestamps(
        "analytics_event_session",
        {"started_at": now - 10, "last_seen_at": now,
         "ended_at": now + 100000})
    assert sess["ended_at"] <= now + worker_mod.MAX_FUTURE_SKEW_SECONDS

    audit = worker_mod._convert_timestamps(
        "admin_audit", {"occurred_at": now + 100000})
    assert audit["occurred_at"] <= now + worker_mod.MAX_FUTURE_SKEW_SECONDS


def test_unknown_event_type_payload_is_untouched():
    p = worker_mod._convert_timestamps("analytics_event",
                                       {"not_at": "2020-01-01"})
    assert p["not_at"] == "2020-01-01"


# ── 4. Readable dead-letter reasons ────────────────────────────────────────

def test_dead_letter_reason_is_short_and_readable():
    from delivery.errors import ErrorClassification
    for code, expected in [
        ("22P02", "Invalid data format"),
        ("42P01", "Database table missing (migration not run?)"),
        ("23505", "Duplicate record"),
    ]:
        cls = ErrorClassification(transient=False, code=code, reason="x")
        reason = worker_mod._readable_dead_letter_reason(cls)
        assert reason == expected
        assert len(reason) <= 60


def test_permanent_failure_stores_full_error_and_readable_reason():
    import psycopg
    from delivery.errors import classify_exception
    item = outbox.enqueue(
        "contact_submission",
        {"reference_code": "DL-1", "occurred_at": time.time()},
        event_key="dl-1")[0]
    claimed = outbox.claim_pending()[0]

    exc = psycopg.errors.InvalidTextRepresentation(
        "invalid input syntax for type uuid: \"boo\"")
    cls = classify_exception(exc)
    worker_mod._handle_failure(claimed, exc, cls)

    with sqlite3.connect(outbox.get_db_path()) as conn:
        row = conn.execute(
            "SELECT dead_letter_reason, last_error_code, last_error_msg "
            "FROM outbox WHERE id = ?", (claimed.id,)).fetchone()
    reason, code, msg = row
    assert reason == "Invalid data format"
    assert code == "22P02"
    assert "invalid input syntax" in (msg or ""), (
        "the full error must be kept separately for developers")
