"""Phase 2 §2.1 security hardening — regression tests.

Covers: SECRET_KEY boot check, new headers (Permissions-Policy payment,
Cross-Origin-Opener-Policy), 30-min idle session timeout, Retry-After on
every 429, the conversion-endpoint rate limit, admin action audit events
(never the token), and unknown-field rejection on admin POSTs.
"""
from __future__ import annotations

import os
import sys
import time
import uuid as _uuid
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

CSRF_TEST_TOKEN = "s" * 43


def _csrf(client):
    with client.session_transaction() as sess:
        sess["anvil_csrf"] = CSRF_TEST_TOKEN
    return {"X-CSRF-Token": CSRF_TEST_TOKEN}


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    from services.rate_limit import (ADMIN_LIMITER, ANALYTICS_LIMITER,
                                     CONTACT_LIMITER, CONVERT_LIMITER)
    for limiter in (ADMIN_LIMITER, ANALYTICS_LIMITER, CONTACT_LIMITER,
                    CONVERT_LIMITER):
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
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


# ── 1. SECRET_KEY boot check ───────────────────────────────────────────────

def test_production_boot_fails_without_secret():
    from app import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"ENV": "production", "SECRET_KEY": ""})


def test_production_boot_fails_with_placeholder_secret():
    from app import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"ENV": "production",
                    "SECRET_KEY": "replace-with-a-random-secret-at-least-32-chars"})


def test_production_boot_fails_with_short_secret():
    from app import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"ENV": "production", "SECRET_KEY": "short-but-20-chars-ok!"})


def test_production_boots_with_strong_secret():
    from app import create_app
    app = create_app({"ENV": "production", "SECRET_KEY": "z" * 48})
    assert app.config["SECRET_KEY"] == "z" * 48


def test_development_keeps_random_fallback():
    from app import create_app
    app = create_app({"ENV": "development", "SECRET_KEY": ""})  # must not raise
    assert len(app.config["SECRET_KEY"]) >= 32


def test_production_env_from_environment_also_fails(monkeypatch):
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "")
    from app import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


# ── 2. Headers ─────────────────────────────────────────────────────────────

def test_new_security_headers_present(client):
    r = client.get("/")
    pp = r.headers.get("Permissions-Policy", "")
    assert "camera=()" in pp and "microphone=()" in pp
    assert "geolocation=()" in pp and "payment=()" in pp
    assert r.headers.get("Cross-Origin-Opener-Policy") == "same-origin"
    csp = r.headers.get("Content-Security-Policy", "")
    assert "unsafe-eval" not in csp, "CSP must never allow unsafe-eval"


# ── 3. Idle timeout ────────────────────────────────────────────────────────

def test_session_expires_after_idle_timeout(client):
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000ee"
        sess["anvil_admin_iat"] = time.time() - 600       # 10 min ago
        sess["anvil_admin_last_seen"] = time.time() - 1900  # 31+ min idle
    r = client.get("/admin/")
    assert r.status_code == 302, "idle session must be expired"


def test_active_session_survives_and_refreshes_last_seen(client):
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000ef"
        sess["anvil_admin_iat"] = time.time() - 600
        sess["anvil_admin_last_seen"] = time.time() - 60  # 1 min idle: fine
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.get("/admin/")
    assert r.status_code == 200
    with client.session_transaction() as sess:
        assert sess["anvil_admin_last_seen"] > time.time() - 5


def test_legacy_session_without_last_seen_uses_iat(client):
    # Sessions created before the idle timeout existed have no last_seen.
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000e0"
        sess["anvil_admin_iat"] = time.time() - 3600  # 1 h ago, idle since then
    r = client.get("/admin/")
    assert r.status_code == 302, "legacy session idle > 30 min must expire"


# ── 4. Retry-After on every 429 ────────────────────────────────────────────

def test_admin_login_throttle_carries_retry_after(client):
    for _ in range(20):
        client.post("/admin/login", json={"access_token": "x"},
                    headers=_csrf(client))
    r = client.post("/admin/login", json={"access_token": "x"},
                    headers=_csrf(client))
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_contact_throttle_carries_retry_after(client):
    payload = {"subject": "s", "message": "m" * 20, "category": "other"}
    for _ in range(5):
        client.post("/api/contact", json=payload)
    r = client.post("/api/contact", json=payload)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_analytics_throttle_carries_retry_after(client):
    from analytics import limiter_identity
    from services.rate_limit import ANALYTICS_LIMITER
    import collections
    key = limiter_identity("", "127.0.0.1")  # the exact key the route uses
    with ANALYTICS_LIMITER._lock:
        ANALYTICS_LIMITER._buckets[key] = collections.deque(
            [time.time() - 1] * ANALYTICS_LIMITER._max)
    ev = {"event_name": "page_view", "session_id": str(_uuid.uuid4())}
    r = client.post("/api/analytics/event", json=ev)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


def test_conversion_rate_limit_and_retry_after(client):
    # 60/min limit: exhaust it, then expect 429 + Retry-After + clean JSON.
    for _ in range(60):
        r = client.post("/api/json-to-xml", json={"text": '{"a":1}'})
        assert r.status_code == 200
    r = client.post("/api/json-to-xml", json={"text": '{"a":1}'})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    body = r.get_json()
    assert body["ok"] is False and "Too many" in body["error"]


# ── 5. Admin action audit trail ────────────────────────────────────────────

def _drain_audit_events():
    return [i for i in outbox.list_recent(limit=100)
            if i.event_type == "admin_audit"]


def test_login_success_and_failure_write_audit_events_without_token(client):
    # Failure first.
    with patch("auth.admin.verify_supabase_token", return_value=None):
        client.post("/admin/login", json={"access_token": "SUPER-SECRET-TOKEN"},
                    headers=_csrf(client))
    # Success.
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000aa",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "SUPER-SECRET-TOKEN"},
                    headers=_csrf(client))

    events = _drain_audit_events()
    actions = [e.payload.get("action") for e in events]
    assert "admin_login_failed" in actions
    assert "admin_login" in actions
    success = next(e for e in events if e.payload.get("action") == "admin_login")
    assert success.payload.get("user_id") == "00000000-0000-0000-0000-0000000000aa"
    # The token must NEVER appear in any audit payload.
    for e in events:
        assert "SUPER-SECRET-TOKEN" not in repr(e.payload)


def test_logout_retry_and_support_update_write_audit_events(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000ab",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "t"},
                    headers=_csrf(client))

    # Enqueue a dead-lettered item to retry.
    outbox.enqueue("analytics_event", {"x": 1}, event_key="audit-me")
    item = outbox.claim_pending()[0]
    outbox.dead_letter(item.id, reason="test")

    import supabase_client
    from contextlib import contextmanager

    @contextmanager
    def fake_cursor():
        class Cur:
            rowcount = 1
            def execute(self, sql, params=()):
                pass
            def fetchone(self):
                return None
            def fetchall(self):
                return []
        yield Cur()

    with patch("auth.admin.is_user_admin", return_value=True), \
         patch.object(supabase_client, "cursor", fake_cursor), \
         patch.object(supabase_client, "is_configured", return_value=True):
        client.post("/admin/queue/retry-all", headers=_csrf(client))
        r = client.post("/admin/queue/%d/retry" % item.id, headers=_csrf(client))
        assert r.status_code in (200, 404)  # already requeued by retry-all
        r = client.post(
            "/admin/support/00000000-0000-0000-0000-0000000000ac",
            json={"status": "in_progress"}, headers=_csrf(client))
        assert r.status_code == 200, r.get_json()
        client.post("/admin/logout", headers=_csrf(client))

    actions = {e.payload.get("action") for e in _drain_audit_events()}
    assert {"admin_login", "admin_queue_retry_all", "admin_queue_retry",
            "admin_support_update", "admin_logout"} <= actions


def test_audit_failure_never_blocks_the_action(client, monkeypatch):
    """A broken outbox must not stop an admin from acting."""
    def broken_enqueue(*a, **kw):
        raise RuntimeError("outbox exploded")
    monkeypatch.setattr("services.audit.enqueue", broken_enqueue)
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000ad",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        r = client.post("/admin/login", json={"access_token": "t"},
                        headers=_csrf(client))
    assert r.status_code == 200, "login must succeed even if auditing fails"


def test_worker_admin_audit_handler_inserts_security_event():
    from delivery.worker import _handle_admin_audit
    from contextlib import contextmanager

    executed = []

    class Cur:
        def execute(self, sql, params=()):
            executed.append((sql, params))

    @contextmanager
    def fake_cursor():
        yield Cur()

    import supabase_client
    item = outbox.enqueue(
        "admin_audit",
        {"action": "admin_login", "user_id": "u-1", "occurred_at": 1700000000,
         "route_name": "/admin/login", "details": "{\"via\": \"test\"}"},
        event_key="audit-handler-test")[0]
    with patch.object(supabase_client, "cursor", fake_cursor):
        _handle_admin_audit(item)
    assert len(executed) == 1
    sql, params = executed[0]
    assert "security_events" in sql
    assert "'admin_action'" in sql
    assert params[1] == "/admin/login"


# ── 6. Unknown-field rejection on admin POSTs ──────────────────────────────

def test_support_update_rejects_unknown_fields(client):
    # Establish an admin session first (the route checks auth before fields).
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-0000000000af"
        sess["anvil_admin_iat"] = time.time()
        sess["anvil_admin_last_seen"] = time.time()
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.post(
            "/admin/support/00000000-0000-0000-0000-0000000000ae",
            json={"status": "new", "is_admin": True, "evil": 1},
            headers=_csrf(client))
    assert r.status_code == 400
    assert "evil" in r.get_json()["error"]


def test_admin_login_rejects_unknown_fields(client):
    r = client.post("/admin/login",
                    json={"access_token": "x", "extra": "y"},
                    headers=_csrf(client))
    assert r.status_code == 400
    assert "extra" in r.get_json()["error"]
