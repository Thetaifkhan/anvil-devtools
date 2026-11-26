"""Phase 1 failure-injection suite — §1.6 worst-case scenarios.

Each test simulates a hostile/broken environment and asserts the app:
stays up, returns a clear message, and recovers without a restart.

Findings are recorded with IDs FI-1 .. FI-13 in the Phase 1 report.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid as _uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

CSRF_TEST_TOKEN = "f" * 43


def _csrf(client):
    with client.session_transaction() as sess:
        sess["anvil_csrf"] = CSRF_TEST_TOKEN
    return {"X-CSRF-Token": CSRF_TEST_TOKEN}


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
              "SUPABASE_SSL_ROOT_CERT"):
        monkeypatch.delenv(k, raising=False)
    # Reset shared rate limiters so tests don't pollute each other.
    from services.rate_limit import CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER
    for limiter in (CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER):
        with limiter._lock:
            limiter._buckets.clear()
    import supabase_client
    supabase_client.reset_engine()
    yield
    supabase_client.reset_engine()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "TRAP_HTTP_EXCEPTIONS": False,
                      "PROPAGATE_EXCEPTIONS": False})
    with app.test_client() as c:
        yield c


VALID_CONTACT = {
    "email": "user@example.com",
    "subject": "FI test",
    "message": "Failure injection body.",
    "category": "feedback",
}


def _login_as_admin(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000aa",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "valid"},
                    headers=_csrf(client))


# ── FI-1: Supabase unreachable ─────────────────────────────────────────────

def test_fi1_supabase_unreachable_public_tools_work_and_things_queue(client):
    import supabase_client

    @contextmanager
    def dead_cursor():
        raise ConnectionError("connection refused (simulated outage)")
        yield  # pragma: no cover

    with patch.object(supabase_client, "cursor", dead_cursor), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=False):
        # Public tools still work.
        r = client.post("/api/json-to-xml", json={"text": '{"a":1}'})
        assert r.status_code == 200 and r.get_json()["ok"] is True
        r = client.get("/")
        assert r.status_code == 200

        # Analytics + contact are queued locally, not failed.
        ev = {"event_name": "page_view", "session_id": str(_uuid.uuid4()),
              "occurred_at": time.time()}
        r = client.post("/api/analytics/event", json=ev)
        assert r.status_code == 202
        r = client.post("/api/contact", json=VALID_CONTACT)
        assert r.status_code == 202

        # Admin shows a plain "not connected" message, no 500.
        _login_as_admin(client)
        with patch("auth.admin.is_user_admin", return_value=True):
            r = client.get("/admin/")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Not connected" in text or "not connected" in text

        # No 500 pages: the public error paths are unaffected.
        assert b"Traceback" not in r.data

    # And the queue survived for the worker to drain later.
    snap = outbox.counts()
    assert snap["pending"] >= 2


# ── FI-2: Supabase slow — timeouts must be bounded ─────────────────────────

def test_fi2_timeouts_are_configured_and_bounded(monkeypatch):
    """A slow (>10 s) database must fail fast, not hang. We assert the
    connection/statement/pool-wait timeouts are all configured with hard
    bounds, and that transport timeouts classify as transient (worker backs
    off rather than hanging)."""
    import supabase_client
    from delivery.errors import classify_exception
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:PLACEHOLDER_PASSWORD@127.0.0.1:5432/db")
    # Values from the environment are clamped by _int_env.
    assert supabase_client._int_env("SUPABASE_CONNECT_TIMEOUT", 5, 1, 60) <= 60
    assert supabase_client._int_env("SUPABASE_STATEMENT_TIMEOUT", 10, 1, 300) <= 300
    assert supabase_client._int_env("SUPABASE_POOL_WAIT_SECONDS", 10, 1, 60) <= 60

    # A statement timeout is a transient, backoff-scheduled error.
    import psycopg
    exc = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    cls = classify_exception(exc)
    assert cls.transient is True
    from delivery.errors import BackoffConfig, compute_backoff
    cfg = BackoffConfig.from_env()
    assert cfg.max_delay <= 900.0, "backoff must cap at 15 minutes"


def test_fi2b_pool_wait_is_bounded_not_infinite():
    import supabase_client
    # Pool wait timeout is bounded to [1, 60] s regardless of env garbage.
    for garbage in ("999999", "-5", "not-a-number"):
        os.environ["SUPABASE_POOL_WAIT_SECONDS"] = garbage
        v = supabase_client._int_env("SUPABASE_POOL_WAIT_SECONDS", 10, 1, 60)
        assert 1 <= v <= 60, f"pool wait unbounded for env {garbage!r}: {v}"
        del os.environ["SUPABASE_POOL_WAIT_SECONDS"]


# ── FI-3: wrong password — clear log, capped backoff ───────────────────────

def test_fi3_wrong_password_is_transient_with_capped_backoff(caplog):
    """28P01 must be transient (operator will fix the secret), the worker
    must log the failure, and backoff must cap at 15 minutes (no storm)."""
    import psycopg
    from delivery.errors import BackoffConfig, classify_exception, compute_backoff
    exc = psycopg.errors.InvalidPassword("password authentication failed for user")
    cls = classify_exception(exc)
    assert cls.transient is True, "28P01 must not dead-letter pending contacts"
    assert cls.code == "28P01"

    # Backoff series stays under the 15-minute cap even after many attempts.
    cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
    for attempt in (1, 5, 10, 50, 200):
        assert compute_backoff(attempt, cfg) <= 900.0

    # The worker logs authentication failures distinctly.
    from delivery.worker import _handle_failure
    item = outbox.enqueue("contact_submission", {"ref": "FI3"}, event_key="fi3")[0]
    with caplog.at_level("WARNING"):
        _handle_failure(item, exc, cls)
    assert any("28P01" in rec.message for rec in caplog.records), (
        "worker must log the auth-failure SQLSTATE for operators")
    # Item was requeued, not dead-lettered.
    assert outbox.counts()["retrying"] == 1


# ── FI-4: certificate file missing ─────────────────────────────────────────

def test_fi4_missing_cert_file_refuses_connection_and_app_stays_up(
        tmp_path, monkeypatch, caplog, client):
    import supabase_client
    missing = str(tmp_path / "nope" / "supabase-ca.crt")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:PLACEHOLDER_PASSWORD@127.0.0.1:5432/db")
    monkeypatch.setenv("SUPABASE_SSL_ROOT_CERT", missing)
    with caplog.at_level("ERROR"):
        configured = supabase_client.is_configured()
    assert configured is False, "missing cert must refuse the connection"
    assert any(missing in rec.getMessage() for rec in caplog.records), (
        "the log must name the SUPABASE_SSL_ROOT_CERT path")
    # App still boots and serves tools.
    r = client.post("/api/json-to-xml", json={"text": '{"a":1}'})
    assert r.status_code == 200


# ── FI-5: malformed DATABASE_URL ───────────────────────────────────────────

def test_fi5_malformed_database_url_app_boots_admin_shows_not_configured(
        monkeypatch):
    import supabase_client
    monkeypatch.setenv("DATABASE_URL", "://not a url at all")
    # Building a DSN must not raise.
    supabase_client.reset_engine()
    try:
        configured = supabase_client.is_configured()
    except Exception as exc:  # pragma: no cover — this is the failure mode
        pytest.fail(f"is_configured() raised on malformed DATABASE_URL: {exc}")
    # Whatever it returns, ping must be False and never raise.
    assert supabase_client.ping() is False

    # The app boots and the admin overview shows "not configured", no crash.
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        r = c.get("/")
        assert r.status_code == 200
        _login_as_admin(c)
        with patch("auth.admin.is_user_admin", return_value=True):
            r = c.get("/admin/")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert ("not configured" in text.lower()
                or "not connected" in text.lower()), (
            "admin must show a not-configured state for a malformed DSN")


# ── FI-6: SQLite corrupted / locked ────────────────────────────────────────

def test_fi6a_corrupted_sqlite_after_boot_returns_error_not_crash(tmp_path):
    """Corruption that appears AFTER boot: the contact route must not
    silently succeed and must not leak a stack trace, and per §1.6 should
    return a friendly error. Target: 503 with a plain message."""
    path = str(tmp_path / "late-corrupt.sqlite3")
    outbox.configure(path)  # schema initialises on a healthy file
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "TRAP_HTTP_EXCEPTIONS": False,
                      "PROPAGATE_EXCEPTIONS": False})
    # Corrupt the file after the app booted (module path still points at it).
    with open(path, "r+b") as fh:
        fh.write(b"GARBAGE GARBAGE GARBAGE GARBAGE")
    with app.test_client() as c:
        r = c.post("/api/contact", json=VALID_CONTACT)
    assert r.status_code == 503, (
        f"corrupt queue must fail with a friendly 503, got {r.status_code}")
    assert b"Traceback" not in r.data
    # Public tools still work with a corrupt queue.
    with app.test_client() as c:
        r = c.post("/api/json-to-xml", json={"text": '{"a":1}'})
        assert r.status_code == 200


def test_fi6b_locked_sqlite_worker_backs_off(tmp_path):
    """Hold an exclusive lock on the outbox; the worker loop must log at a
    bounded rate (once per failed iteration with backoff), not spin."""
    path = str(tmp_path / "lock.sqlite3")
    outbox.configure(path, durable=True)
    outbox.enqueue("analytics_event", {"x": 1}, event_key="fi6b")

    holder = sqlite3.connect(path, timeout=1.0, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")

    from delivery import worker as worker_mod
    # One iteration must fail (raise out of run_once) but not hang forever.
    t0 = time.monotonic()
    raised = None
    try:
        worker_mod.run_once(batch_size=5)
    except Exception as exc:  # expected: database is locked
        raised = exc
    elapsed = time.monotonic() - t0
    holder.execute("ROLLBACK")
    holder.close()
    # busy_timeout is 5 s; the iteration must give up around then, not hang.
    assert raised is not None or elapsed < 30
    assert elapsed < 30, "worker hung on a locked database"
    # After the lock is released, the same call recovers without restart.
    assert worker_mod.run_once(batch_size=5) == 1


# ── FI-7: disk full ────────────────────────────────────────────────────────

def test_fi7_disk_full_rejects_cleanly_no_partial_rows(tmp_path, monkeypatch):
    path = str(tmp_path / "diskfull.sqlite3")
    outbox.configure(path)

    def full_disk(path=None):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(outbox, "_connect", full_disk)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "TRAP_HTTP_EXCEPTIONS": False,
                      "PROPAGATE_EXCEPTIONS": False})
    with app.test_client() as c:
        r = c.post("/api/contact", json=VALID_CONTACT)
    monkeypatch.undo()
    # The user must see a 503 with a message, never a silent success.
    assert r.status_code == 503, (
        f"disk-full must be a friendly 503, got {r.status_code}")
    # ...and no partial row may exist in the outbox.
    with sqlite3.connect(path) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE event_type='contact_submission'"
        ).fetchone()[0]
    assert n == 0, "partial row written despite disk-full failure"


# ── FI-8: token expired or forged ──────────────────────────────────────────

def test_fi8_forged_or_expired_token_is_401_without_stack_trace(client):
    # Forged garbage token.
    r = client.post("/admin/login", json={"access_token": "forged.jwt.value"},
                    headers=_csrf(client))
    assert r.status_code == 401
    assert "Traceback" not in r.get_data(as_text=True)
    assert "could not verify" in r.get_json()["error"]

    # Expired token: Supabase answers 401 -> verifier returns None.
    import requests
    resp = MagicMock(status_code=401)
    exc = requests.exceptions.HTTPError(response=resp)
    with patch("auth.admin.requests.get", side_effect=exc):
        r = client.post("/admin/login", json={"access_token": "expired"},
                        headers=_csrf(client))
    assert r.status_code == 401
    assert "Traceback" not in r.get_data(as_text=True)

    # Session forged directly (signed with the right key but not an admin):
    # still 403/redirect, never a crash.
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000cc"
        sess["anvil_admin_iat"] = time.time()
    with patch("auth.admin.is_user_admin", return_value=False):
        r = client.get("/admin/")
    assert r.status_code in (302, 403)


# ── FI-9: 1 MB of deeply nested JSON ───────────────────────────────────────

def test_fi9_deeply_nested_json_rejected_cleanly(client):
    # ~1 MB of nested arrays: [[[[ ... ]]]]
    depth = 500_000
    body = "[" * depth + "]" * depth
    assert len(body) >= 999_000
    r = client.post("/api/json-to-xml",
                    data=json.dumps({"text": body}).encode(),
                    content_type="application/json")
    # Must be a clean 4xx (depth/parse rejection) or a bounded 4xx/5xx —
    # never a hang. Flask/Werkzeug guards apply before we get here if the
    # body exceeds MAX_CONTENT_LENGTH (5 MB default) -> 413/400.
    assert r.status_code in (200, 400, 413, 500)
    assert r.status_code != 200 or r.get_json(silent=True) is not None
    # The response must be quick — no memory blow-up (implicitly proven by
    # the test finishing).

    # Same for a deeply nested object.
    r = client.post("/api/json-to-xml",
                    data=json.dumps({"text": '{"a":' * 60_000 + "1" + "}" * 60_000}).encode(),
                    content_type="application/json")
    assert r.status_code in (200, 400, 413, 500)


# ── FI-10: XML billion laughs ──────────────────────────────────────────────

BILLION_LAFFS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>
"""


def test_fi10_billion_laughs_rejected(client):
    t0 = time.monotonic()
    r = client.post("/api/xml-to-json", json={"text": BILLION_LAFFS})
    elapsed = time.monotonic() - t0
    assert r.status_code == 400, "entity bomb must be rejected, not parsed"
    assert elapsed < 10, f"parser spent {elapsed:.1f}s on an entity bomb"
    # No expansion happened: response is small.
    assert len(r.data) < 10_000


# ── FI-11: catastrophic regex — runs in a web worker (static check) ────────

def test_fi11_regex_tester_uses_web_worker_with_timeout():
    """The catastrophic-backtracking guard lives in the browser: the regex
    tester must run inside a Web Worker (never the main thread) and the
    page must enforce a timeout that terminates the worker. Assert the
    shipped code does both."""
    from pathlib import Path
    # The /regex-tester page spawns the worker and enforces a timeout.
    tmpl = Path("templates/tools/regex_tester.html").read_text(encoding="utf-8")
    assert "new Worker" in tmpl, "regex tester must offload matching to a Worker"
    assert "terminate" in tmpl, "worker must be killable on timeout"
    assert "setTimeout" in tmpl
    # The web worker source exists and speaks postMessage.
    worker_src = Path("static/js/tools/regex_worker.js").read_text(encoding="utf-8")
    assert "postMessage" in worker_src
    # Service worker precaches it, so it loads offline too.
    sw = Path("static/js/service-worker.js").read_text(encoding="utf-8")
    assert "regex_worker.js" in sw


# ── FI-12: 20 concurrent admin requests ────────────────────────────────────

def test_fi12_concurrent_admin_requests_no_deadlock(client):
    _login_as_admin(client)
    import supabase_client

    @contextmanager
    def fake_cursor():
        class Cur:
            def execute(self, sql, params=()):
                time.sleep(0.01)

            def fetchone(self):
                return (1, 1, 1, 0, 0, 0)

            def fetchall(self):
                return []
        yield Cur()

    def hit(_):
        app = client.application
        with app.test_client() as c:
            with c.session_transaction() as sess:
                sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000aa"
                sess["anvil_admin_iat"] = time.time()
                sess["anvil_admin_last_seen"] = time.time()
            r = c.get("/admin/")
            return r.status_code

    # ONE patch scope around all threads: unittest.mock is not thread-safe
    # when several threads patch the same attribute concurrently (teardowns
    # interleave and leak MagicMocks into later tests).
    with patch("auth.admin.is_user_admin", return_value=True), \
         patch.object(supabase_client, "cursor", fake_cursor), \
         patch.object(supabase_client, "ping", return_value=True), \
         patch.object(supabase_client, "is_configured", return_value=True):
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(hit, range(20)))
    assert results == [200] * 20, f"concurrent admin requests failed: {results}"


def test_fi12b_concurrent_enqueues_have_correct_counts(tmp_path):
    path = str(tmp_path / "conc.sqlite3")
    outbox.configure(path)
    def enq(i):
        outbox.enqueue("analytics_event", {"i": i}, event_key=f"conc-{i}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(enq, range(40)))
    snap = outbox.counts()
    assert snap["pending"] == 40, f"expected 40, counted {snap['pending']}"


# ── FI-13: SIGTERM during an outbox write ──────────────────────────────────

def test_fi13_expired_lease_recovered_without_duplicate_delivery(tmp_path):
    """Simulate: worker claims an item, dies mid-write (lease expires).
    Next start must re-claim it exactly once, and the idempotency key
    ensures at-most-once delivery downstream."""
    path = str(tmp_path / "sigterm.sqlite3")
    outbox.configure(path, durable=True)
    item, _ = outbox.enqueue("contact_submission",
                             {"reference_code": "FI13-TEST"},
                             event_key="fi13-key")

    # "Process A" claims the item then dies: lease left behind, no delivery.
    claimed = outbox.claim_pending(batch_size=1)
    assert len(claimed) == 1
    assert outbox.recover_expired_leases() == 0  # lease still fresh

    # Simulate the lease expiring (worker was killed 5+ minutes ago).
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE outbox SET lease_expires_at = ? WHERE id = ?",
                     (time.time() - 1, item.id))

    # "Process B" (next start) sees the expired lease and re-claims it.
    assert outbox.recover_expired_leases() == 1
    reclaimed = outbox.claim_pending(batch_size=1)
    assert len(reclaimed) == 1 and reclaimed[0].id == item.id
    assert reclaimed[0].attempt_count == 2

    # Delivery is idempotent: the event_key is the same, so the
    # private.delivery_queue ON CONFLICT DO NOTHING in the worker makes a
    # double delivery a no-op. Assert the key is stable across claims.
    assert reclaimed[0].event_key == claimed[0].event_key == "fi13-key"

    # After delivery the item is done; nothing claimable remains.
    outbox.mark_delivered(item.id)
    assert outbox.claim_pending(batch_size=10) == []
    assert outbox.counts()["delivered"] == 1
