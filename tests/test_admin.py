"""Tests for admin auth + admin route authorization.

Covers:
- Admin routes reject unauthenticated users.
- Admin routes reject authenticated-but-non-admin users.
- /admin/login page renders.
- /admin/logout clears the session.
- The `require_admin` decorator returns 401 JSON for /api/* routes.
- The auth module never trusts a client-supplied UUID alone.
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

CSRF_TEST_TOKEN = "t" * 43


def _csrf(client):
    """Simulate a browser that already holds this session's CSRF token.

    The token is seeded into the session and echoed in the header, exactly as
    admin.js does. Tests stay honest about what the server checks.
    """
    with client.session_transaction() as sess:
        sess["anvil_csrf"] = CSRF_TEST_TOKEN
    return {"X-CSRF-Token": CSRF_TEST_TOKEN}



@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    # Ensure no real Supabase connection is attempted during tests.
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Reset the cached engine so the env changes take effect.
    import supabase_client
    supabase_client.reset_engine()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


# ── Auth gating ───────────────────────────────────────────────────────────

def test_admin_root_redirects_to_login_when_unauthenticated(client):
    r = client.get("/admin/")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["Location"]


def test_admin_traffic_redirects_when_unauthenticated(client):
    r = client.get("/admin/traffic")
    assert r.status_code == 302


def test_admin_support_inbox_redirects_when_unauthenticated(client):
    r = client.get("/admin/support")
    assert r.status_code == 302


def test_admin_audit_redirects_when_unauthenticated(client):
    r = client.get("/admin/audit")
    assert r.status_code == 302


def test_admin_security_redirects_when_unauthenticated(client):
    r = client.get("/admin/security")
    assert r.status_code == 302


def test_admin_queue_redirects_when_unauthenticated(client):
    r = client.get("/admin/queue")
    assert r.status_code == 302


def test_admin_login_page_renders(client):
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert b"admin-login-form" in r.data


def test_admin_login_rejects_empty_token(client):
    r = client.post("/admin/login", json={}, headers=_csrf(client))
    assert r.status_code == 400
    assert r.get_json()["ok"] is False


def test_admin_login_rejects_invalid_token(client, monkeypatch):
    # Patch the verifier to return None.
    with patch("auth.admin.verify_supabase_token", return_value=None):
        r = client.post("/admin/login", json={"access_token": "invalid"}, headers=_csrf(client))
        assert r.status_code == 401


def test_admin_login_rejects_verified_non_admin(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-000000000001",
                          email="user@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=False):
        r = client.post("/admin/login", json={"access_token": "valid"}, headers=_csrf(client))
        assert r.status_code == 403
        assert "not configured as an Anvil admin" in r.get_json()["error"]


def test_admin_login_success_creates_session(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-000000000002",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        r = client.post("/admin/login", json={"access_token": "valid"}, headers=_csrf(client))
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "/admin" in data["redirect"]


def test_logout_clears_session(client):
    # Start a fake session directly via the test client.
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-000000000003"
        sess["anvil_admin_iat"] = time.time()
    # Now log out.
    r = client.post("/admin/logout", headers=_csrf(client), follow_redirects=False)
    assert r.status_code == 302
    # After logout, /admin/ should redirect to login.
    r = client.get("/admin/")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["Location"]


def test_session_expires_after_ttl(client):
    # Set issued_at to long ago.
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-000000000004"
        sess["anvil_admin_iat"] = time.time() - 10 * 3600  # 10 hours ago
    # Even with a session, the TLL check should fail.
    r = client.get("/admin/")
    assert r.status_code == 302


def test_authenticated_admin_can_view_overview(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-000000000005",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        # Log in to create the session.
        client.post("/admin/login", json={"access_token": "valid"}, headers=_csrf(client))
    # Now access the overview page. Patch again — the @require_admin
    # decorator re-checks on every request.
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.get("/admin/")
    assert r.status_code == 200


def test_api_admin_healthz_requires_auth(client):
    r = client.get("/api/admin/healthz")
    assert r.status_code == 401


def test_api_admin_healthz_works_for_admin(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-000000000006",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "valid"}, headers=_csrf(client))
    with patch("auth.admin.is_user_admin", return_value=True):
        r = client.get("/api/admin/healthz")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert "queue" in data


def test_admin_login_throttles_after_many_attempts(client):
    """The login route is rate-limited to prevent brute-force attacks."""
    # The first 20 attempts should not be 429.
    for _ in range(20):
        client.post("/admin/login", json={"access_token": "garbage"}, headers=_csrf(client))
    # The 21st should be throttled.
    r = client.post("/admin/login", json={"access_token": "garbage"}, headers=_csrf(client))
    assert r.status_code == 429
