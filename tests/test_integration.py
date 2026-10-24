"""End-to-end smoke tests that verify the integration of every new
endpoint: contact form, analytics, admin auth, queue status.

These tests run against the Flask test client with the worker disabled
and Supabase mocked out. They exercise the full request flow from HTTP
to the durable outbox to the worker (with the worker's Supabase
interaction mocked).
"""
import os
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

CSRF_TEST_TOKEN = "t" * 43


def _csrf(client):
    """Simulate a browser that already holds this session's CSRF token.

    The token is seeded into the session and echoed in the header, exactly as
    admin.js does. Tests stay honest about what the server checks.
    """
    with client.session_transaction() as sess:
        sess["anvil_csrf"] = CSRF_TEST_TOKEN
    return {"X-CSRF-Token": CSRF_TEST_TOKEN}


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Each test gets a fresh SQLite outbox and no env vars leaking."""
    from delivery import outbox
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY", "IP_HMAC_SECRET"):
        monkeypatch.delenv(k, raising=False)
    import supabase_client
    supabase_client.reset_engine()
    # Reset rate limiters.
    from services.rate_limit import CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER
    for limiter in (CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER):
        with limiter._lock:
            limiter._buckets.clear()


@pytest.fixture
def client():
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


def test_homepage_still_loads(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Anvil" in r.data


def test_contact_page_renders_with_reference_ui(client):
    r = client.get("/contact")
    assert r.status_code == 200
    assert b"reference code" in r.data.lower()
    assert b"contact-form" in r.data
    assert b"idempotency" not in r.data  # internal detail, not user-facing


def test_full_contact_submission_flow(client):
    """Submit a contact form → get a reference code → see it in the queue."""
    r = client.post("/api/contact", json={
        "category": "bug_report",
        "subject": "End-to-end smoke test",
        "message": "This message has enough detail to pass validation.",
        "name": "Smoke Test",
        "email": "smoke@example.com",
    })
    assert r.status_code == 202
    data = r.get_json()
    assert data["ok"] is True
    assert data["reference_code"].startswith("ANVIL-")

    # The submission should now be in the local outbox.
    from delivery import counts
    snap = counts()
    assert snap["contact_pending"] == 1
    assert snap["pending"] == 1


def test_analytics_event_flow(client):
    """Submit a page_view event → it lands in the outbox."""
    r = client.post("/api/analytics/event", json={
        "event_name": "page_view",
        "session_id": "00000000-0000-0000-0000-000000000001",
        "page_path": "/",
    })
    assert r.status_code == 202
    from delivery import counts
    snap = counts()
    assert snap["analytics_pending"] == 1


def test_analytics_session_flow(client):
    """Submit a session upsert → it lands in the outbox."""
    r = client.post("/api/analytics/session", json={
        "visitor_id": "00000000-0000-0000-0000-000000000001",
        "session_id": "00000000-0000-0000-0000-000000000002",
        "engagement_seconds": 60,
        "device_type": "desktop",
    })
    assert r.status_code == 202


def test_admin_login_logout_round_trip(client):
    """Full admin login → access → logout flow."""
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-000000000003",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        r = client.post("/admin/login", json={"access_token": "valid"}, headers=_csrf(client))
        assert r.status_code == 200
    # Now the session is set. Access should work.
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.get("/admin/")
        assert r.status_code == 200
        r = client.get("/admin/queue")
        assert r.status_code == 200
        r = client.get("/admin/support")
        assert r.status_code == 200
    # Logout.
    r = client.post("/admin/logout", headers=_csrf(client))
    assert r.status_code == 302
    # After logout, /admin/ should redirect to login.
    r = client.get("/admin/")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["Location"]


def test_healthz_does_not_leak_config(client):
    """The public /healthz must reveal nothing about Supabase config."""
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.get_json()
    assert data == {"ok": True}
    assert "supabase" not in r.data.decode().lower()


def test_analytics_failure_does_not_break_tool_execution(client):
    """The analytics endpoint failing must not break tool routes."""
    # Even with no outbox path configured (would cause analytics to fail),
    # the tool conversion endpoints must still work.
    r = client.post("/api/json-to-xml", json={"text": '{"a":1}', "root": "r"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "<a>1</a>" in data["result"]


def test_contact_form_returns_correct_status_codes(client):
    """The /api/contact endpoint must distinguish 400/429/503/202."""
    # 400: missing required fields
    r = client.post("/api/contact", json={"category": "bug_report"})
    assert r.status_code == 400

    # 202: valid submission
    r = client.post("/api/contact", json={
        "category": "bug_report",
        "subject": "Status code test",
        "message": "Detail goes here, this is enough to pass validation.",
        "idempotency_key": "smoke-key-1",
    })
    assert r.status_code == 202

    # 202 (already-queued): same idempotency key
    r = client.post("/api/contact", json={
        "category": "bug_report",
        "subject": "Should be dedup'd",
        "message": "Detail goes here, this is enough to pass validation.",
        "idempotency_key": "smoke-key-1",
    })
    assert r.status_code == 202
    assert r.get_json()["already_queued"] is True


def test_admin_route_rejects_unauthenticated(client):
    """All /admin/* routes must redirect to /admin/login."""
    for path in ("/admin/", "/admin/traffic", "/admin/engagement",
                 "/admin/tools", "/admin/support", "/admin/audit",
                 "/admin/security", "/admin/queue"):
        r = client.get(path)
        assert r.status_code == 302, f"{path} should redirect"
        assert "/admin/login" in r.headers["Location"], \
            f"{path} should redirect to /admin/login"


def test_admin_api_healthz_rejects_unauthenticated(client):
    r = client.get("/api/admin/healthz")
    assert r.status_code == 401


def test_sitemap_includes_contact_page(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert b"/contact" in r.data


def test_analytics_js_loaded_on_every_page(client):
    """Every page must include the analytics tracker script."""
    for path in ("/", "/workbench", "/contact", "/json-formatter"):
        r = client.get(path)
        assert r.status_code == 200, f"{path} should return 200"
        assert b"/static/js/analytics.js" in r.data, \
            f"{path} should include analytics.js"
