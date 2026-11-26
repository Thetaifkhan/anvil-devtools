"""Phase 1 validation suite — explicit coverage for the §1.3 functional table.

Each test maps to one row of the validation matrix:
- Public tools: every route in tool_registry returns 200 on GET.
- Server-backed tools: valid input -> JSON ok; invalid input -> clear 400.
- Contact form: valid -> reference code; missing fields -> 400; rate limit -> 429.
- Analytics: 202 for valid event + session payloads; _to_ts epoch -> aware dt.
- Outbox: enqueue -> claim -> deliver / fail+backoff / dead-letter / requeue,
  and survival across a simulated restart (close + reopen SQLite).
- Admin: login needs Supabase JWT AND admin row; non-admin 403; logged-out
  redirect; pages 200 with a fake Supabase.
- Admin retry-all: requires CSRF and admin, redirects to /admin/.
- Security headers incl. the HSTS-only-on-https gate.
- CSRF on every state-changing admin POST.
- Input limits: 413 over MAX_CONTENT_LENGTH; MAX_TEXT_CHARS clean reject.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox
from tool_registry import TOOLS

CSRF_TEST_TOKEN = "p" * 43


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
    # Reset shared module-level rate limiters so tests don't pollute each
    # other (same pattern as tests/test_contact.py).
    from services.rate_limit import CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER
    for limiter in (CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER):
        with limiter._lock:
            limiter._buckets.clear()
    import supabase_client
    supabase_client.reset_engine()
    yield
    # Reset any DB env leakage between tests.
    supabase_client.reset_engine()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


# ── 1. Public tools: every registry route returns 200 ─────────────────────

def test_every_tool_route_returns_200(client):
    for tool in TOOLS:
        r = client.get(f"/{tool['slug']}")
        assert r.status_code == 200, f"/{tool['slug']} returned {r.status_code}"
        assert r.headers.get("Content-Type", "").startswith("text/html")


def test_every_server_backed_tool_endpoint_valid_and_invalid(client):
    cases = [
        ("/api/json-to-xml", {"text": '{"a": 1}'}, {"text": "{oops"}),
        ("/api/xml-to-json", {"text": "<a>1</a>"}, {"text": "<a>"}),
        ("/api/xml-format", {"text": "<a><b/></a>"}, {"text": "not xml <?"}),
        ("/api/xml-validate", {"text": "<a/>"}, None),
        ("/api/json-to-yaml", {"text": '{"a": 1}'}, {"text": "[1,2"}),
        ("/api/yaml-to-json", {"text": "a: 1"}, {"text": "a: [unclosed"}),
        ("/api/csv-to-json", {"text": "a,b\n1,2"}, {"text": ""}),
        ("/api/json-to-csv", {"text": '[{"a": 1}]'}, {"text": "nope"}),
        ("/api/sql-format", {"text": "select 1", "action": "format"},
                            {"text": "select 1", "action": "bogus"}),
    ]
    validate_endpoints = {"/api/xml-validate"}
    for endpoint, valid, invalid in cases:
        r = client.post(endpoint, json=valid)
        assert r.status_code == 200, f"{endpoint} valid input -> {r.status_code}"
        body = r.get_json()
        assert body["ok"] is True, f"{endpoint} ok flag: {body}"
        if endpoint in validate_endpoints:
            assert body.get("valid") is True and body.get("message"), (
                f"{endpoint} validator must return a verdict + message")
        else:
            assert "result" in body and body["result"] != "", (
                f"{endpoint} returned empty result on success")
        if invalid is not None:
            r = client.post(endpoint, json=invalid)
            assert r.status_code == 400, f"{endpoint} invalid input -> {r.status_code}"
            body = r.get_json()
            assert body["ok"] is False and body.get("error"), (
                f"{endpoint} invalid input must return a readable error")


# ── 2. Contact form ────────────────────────────────────────────────────────

VALID_CONTACT = {
    "email": "user@example.com",
    "subject": "Feedback on the JSON formatter",
    "message": "The formatter works well. Thanks for building it.",
    "category": "feedback",
}


def test_contact_valid_returns_reference_code(client):
    r = client.post("/api/contact", json=VALID_CONTACT)
    assert r.status_code == 202
    data = r.get_json()
    assert data["ok"] is True
    assert data.get("reference_code"), "no reference code returned"
    assert data.get("delivery_status") in ("queued_durable", "queued_volatile")


def test_contact_missing_required_fields_return_400_with_readable_message(client):
    # Required fields per services/contact_service.validate_submission:
    # category, subject, message. Email is OPTIONAL by design (anonymous
    # feedback is accepted); when present it must be well-formed.
    for missing in ("subject", "message", "category"):
        payload = dict(VALID_CONTACT)
        del payload[missing]
        r = client.post("/api/contact", json=payload)
        assert r.status_code == 400, f"missing {missing} -> {r.status_code}"
        assert r.get_json()["error"], f"missing {missing}: no readable message"
    # A malformed email (present but invalid) must be a 400 too.
    bad = dict(VALID_CONTACT, email="not-an-email")
    r = client.post("/api/contact", json=bad)
    assert r.status_code == 400


def test_contact_rate_limit_returns_429(client):
    # Limiter: 5 per 600 s per client IP.
    for _ in range(5):
        client.post("/api/contact", json=VALID_CONTACT)
    r = client.post("/api/contact", json=VALID_CONTACT)
    assert r.status_code == 429
    assert r.get_json()["error"]


# ── 3. Analytics ───────────────────────────────────────────────────────────

def test_analytics_event_and_session_return_202(client):
    import uuid as _uuid
    ev = {
        "event_name": "page_view",
        "session_id": str(_uuid.uuid4()),
        "occurred_at": time.time(),
        "page_path": "/",
    }
    r = client.post("/api/analytics/event", json=ev)
    assert r.status_code == 202, r.get_json()

    sess = {
        "session_id": str(_uuid.uuid4()),
        "visitor_id": str(_uuid.uuid4()),
        "started_at": time.time(),
        "last_seen_at": time.time(),
        "engagement_seconds": 12,
        "landing_page": "/",
    }
    r = client.post("/api/analytics/session", json=sess)
    assert r.status_code == 202, r.get_json()


def test_analytics_invalid_payload_returns_400(client):
    r = client.post("/api/analytics/event", json={"bogus": True})
    assert r.status_code == 400


def test_to_ts_converts_epoch_to_timezone_aware_datetime():
    from datetime import datetime
    from delivery.worker import _to_ts
    out = _to_ts(1700000000)
    assert isinstance(out, datetime)
    assert out.tzinfo is not None, "worker must store tz-aware datetimes"
    assert _to_ts(1700000000.5).tzinfo is not None
    assert _to_ts(None) is None
    dt = datetime(2020, 1, 1)
    assert _to_ts(dt) is dt  # datetimes pass through


# ── 4. Outbox lifecycle + restart survival ─────────────────────────────────

def test_outbox_full_lifecycle_and_restart_survival(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "1000")
    path = str(tmp_path / "restart.sqlite3")
    outbox.configure(path, durable=True)

    item, created = outbox.enqueue("analytics_event", {"x": 1}, event_key="k1")
    assert created is True

    claimed = outbox.claim_pending(batch_size=10)
    assert [i.id for i in claimed] == [item.id]
    assert claimed[0].attempt_count == 1

    outbox.mark_failed(item.id, error_code="operational",
                       error_msg="connection refused",
                       next_attempt_at=time.time() + 1)
    snap = outbox.counts()
    assert snap["retrying"] == 1

    # Force the retry to be due now, claim again, and deliver.
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE outbox SET next_attempt_at = 0 WHERE id = ?", (item.id,))
    claimed = outbox.claim_pending()
    assert len(claimed) == 1
    outbox.mark_delivered(item.id)

    # Dead-letter path.
    item2, _ = outbox.enqueue("analytics_event", {"x": 2}, event_key="k2")
    claimed = outbox.claim_pending()
    assert len(claimed) == 1 and claimed[0].id == item2.id
    outbox.dead_letter(item2.id, reason="permanent_failure:22P02")
    assert outbox.counts()["dead_lettered"] == 1

    # Requeue the dead letter.
    assert outbox.requeue_dead_letter(item2.id) is None
    snap = outbox.counts()
    assert snap["dead_lettered"] == 0 and snap["pending"] == 1

    # Simulated restart: brand-new module state, same env (SUPABASE_QUEUE_PATH
    # still points at the file). The pending item must still be there and
    # claimable, with history preserved.
    monkeypatch.setenv("SUPABASE_QUEUE_PATH", path)
    was = outbox._DB_PATH
    outbox._DB_PATH = None
    outbox._DURABILITY_VERIFIED = False
    try:
        assert outbox.get_db_path() == path  # re-reads env -> same path
        claimed = outbox.claim_pending()
        assert len(claimed) == 1, "pending item lost across simulated restart"
        assert claimed[0].event_key == "k2"
        assert outbox.counts()["delivered"] == 1, "delivery history lost"
    finally:
        outbox._DB_PATH = was


# ── 5. Admin auth matrix ───────────────────────────────────────────────────

def _login_as_admin(client, uid="00000000-0000-0000-0000-0000000000aa"):
    fake_user = MagicMock(user_id=uid, email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "valid"},
                    headers=_csrf(client))


def test_admin_requires_valid_jwt_and_admin_row(client):
    # No token at all.
    with patch("auth.admin.verify_supabase_token", return_value=None):
        r = client.post("/admin/login", json={"access_token": "forged"},
                        headers=_csrf(client))
    assert r.status_code == 401
    assert "Traceback" not in r.get_data(as_text=True)

    # Valid JWT but no admin_profiles row.
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000bb")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=False):
        r = client.post("/admin/login", json={"access_token": "valid"},
                        headers=_csrf(client))
    assert r.status_code == 403


class _FakeCursor:
    """Minimal fake for supabase_client.cursor() that returns canned rows."""

    def __init__(self, rows):
        self._rows = rows
        self._consumed = False
        self.rowcount = len(rows)

    def execute(self, sql, params=()):
        self._sql = sql
        return self

    def fetchone(self):
        if self._rows and not self._consumed:
            self._consumed = True
            return self._rows[0]
        return self._rows[0] if self._rows else None

    def fetchall(self):
        self._consumed = True
        return list(self._rows)


@contextmanager
def _fake_supabase(rows_for):
    """Patch supabase_client.cursor so each ``with`` returns the next canned
    cursor (or the last one). rows_for is a list of row-lists."""
    import supabase_client
    calls = {"n": 0}

    @contextmanager
    def fake_cursor():
        n = min(calls["n"], len(rows_for) - 1)
        calls["n"] += 1
        yield _FakeCursor(rows_for[n])

    with patch.object(supabase_client, "cursor", fake_cursor), \
         patch.object(supabase_client, "ping", return_value=True), \
         patch.object(supabase_client, "is_configured", return_value=True):
        yield


def test_admin_pages_return_200_with_supabase_up(client):
    _login_as_admin(client)
    kpi_row = (3, 2, 2, 1, 1, 0)  # events, sessions, active, open, new, security
    empty = ()
    with patch("auth.admin.is_user_admin", return_value=True), \
         _fake_supabase([ [kpi_row], [empty], [empty], [empty],
                          [(0,)], [empty], [empty] ]):
        for path in ("/admin/", "/admin/traffic", "/admin/engagement",
                     "/admin/tools", "/admin/support", "/admin/audit",
                     "/admin/security", "/admin/queue"):
            r = client.get(path)
            assert r.status_code == 200, f"{path} -> {r.status_code}"
            assert b"Traceback" not in r.data


def test_admin_shows_not_connected_when_supabase_down(client):
    """DB unreachable: overview must render a plain 'not connected' state,
    never a 500 page."""
    _login_as_admin(client)
    import supabase_client

    @contextmanager
    def exploding_cursor():
        raise ConnectionError("Supabase unreachable")
        yield  # pragma: no cover

    with patch("auth.admin.is_user_admin", return_value=True), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=False), \
         patch.object(supabase_client, "cursor", exploding_cursor):
        r = client.get("/admin/")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert "Not connected" in text or "not connected" in text, (
        "overview must say the database is not connected in plain words")


# ── 6. Admin retry-all ─────────────────────────────────────────────────────

def test_retry_all_requires_csrf_and_admin_and_redirects(client):
    # Unauthenticated -> login redirect.
    r = client.post("/admin/queue/retry-all")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["Location"]

    # Authenticated, no CSRF token -> 403.
    _login_as_admin(client)
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.post("/admin/queue/retry-all")
    assert r.status_code == 403

    # Authenticated + CSRF -> redirect to /admin/.
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.post("/admin/queue/retry-all", headers=_csrf(client))
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/admin/")


# ── 7. Security headers ────────────────────────────────────────────────────

def test_security_headers_on_every_html_response(client):
    r = client.get("/")
    h = r.headers
    assert "Content-Security-Policy" in h
    assert h.get("X-Content-Type-Options") == "nosniff"
    assert "strict-origin" in h.get("Referrer-Policy", "")
    assert h.get("X-Frame-Options") == "DENY" or "frame-ancestors" in h.get(
        "Content-Security-Policy", "")
    # Default SITE_URL is http://localhost:5000 -> no HSTS.
    assert "Strict-Transport-Security" not in h


def test_hsts_only_when_site_url_is_https(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    monkeypatch.setenv("SITE_URL", "https://anvil.example.com")
    monkeypatch.setenv("FLASK_ENV", "production")
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "SITE_URL": "https://anvil.example.com",
                      "ENV": "production"})
    with app.test_client() as c:
        r = c.get("/")
    assert "max-age" in r.headers.get("Strict-Transport-Security", "")
    # Session cookie must be flagged Secure on the https site.
    assert app.config.get("SESSION_COOKIE_SECURE") is True


# ── 8. CSRF on every state-changing admin POST ─────────────────────────────

def test_every_state_changing_admin_post_requires_csrf(client):
    _login_as_admin(client)
    posts = [
        "/admin/logout",
        "/admin/queue/retry-all",
        "/admin/queue/1/retry",
        "/admin/support/00000000-0000-0000-0000-0000000000ff",
    ]
    with patch("auth.admin.is_user_admin", return_value=True):
        for path in posts:
            r = client.post(path, json={})
            assert r.status_code == 403, f"{path} without CSRF -> {r.status_code}"


# ── 9. Input limits ────────────────────────────────────────────────────────

def test_body_over_max_content_length_returns_413(monkeypatch):
    """Bodies over MAX_CONTENT_LENGTH must return 413 (Phase 2 fix for
    finding F-413: routes used to swallow the Werkzeug HTTPException and
    return a generic 500)."""
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "MAX_CONTENT_LENGTH": 1024,
                      "TRAP_HTTP_EXCEPTIONS": False})
    with app.test_client() as c:
        r = c.post("/api/json-to-xml", data=b"x" * 2048,
                   content_type="application/json")
    assert r.status_code == 413, (
        f"413 expected, got {r.status_code} (routes swallow HTTPException)")
    assert r.get_json()["ok"] is False


def test_text_over_max_chars_rejected_cleanly(client):
    # Default MAX_TEXT_CHARS is 2,000,000; send something clearly larger
    # without hitting the 5 MB body limit: 2,100,000 'a' chars ~ 2.1 MB.
    r = client.post("/api/json-to-xml", json={"text": '{"t": "' + "a" * 2_100_000 + '"}'})
    assert r.status_code == 400
    assert "too large" in r.get_json()["error"]


# ── 10. Workbench ops sanity (server-side pieces) ──────────────────────────

def test_workbench_page_loads_and_registry_covers_all_operations(client):
    r = client.get("/workbench")
    assert r.status_code == 200
    from tool_registry import CAPABILITY_MAP, OPERATIONS
    used_ops = {v[0] for v in CAPABILITY_MAP.values()}
    assert used_ops <= set(OPERATIONS), f"unknown operations: {used_ops - set(OPERATIONS)}"
    # Every operation named in the §1.3 table is represented by a tool.
    # (The registry calls the diff *operation* "diff"; its action verb is
    # "compare".)
    used_ops = {t.get("operation") for t in TOOLS}
    for op in ("formatter", "validator", "converter", "encoder", "decoder",
               "diff", "generator"):
        assert op in used_ops, f"no tool for operation {op}"
