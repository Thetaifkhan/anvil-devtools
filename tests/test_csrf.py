"""CSRF protection for admin state-changing endpoints.

Each write endpoint must reject requests without this session's token, and
must still reject authenticated-but-unauthorized callers first (401/redirect
before CSRF). Admin pages must embed the token so the real UI can use it.
"""
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

SUPPORT_ID = "9b2c1f3e-4d5a-4b6c-8d7e-0f1a2b3c4d5e"
TOKEN = "k" * 43


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    import supabase_client
    supabase_client.reset_engine()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    return app.test_client()


def _admin_session(client, token=TOKEN, with_token=True):
    with client.session_transaction() as sess:
        sess["anvil_admin_uid"] = "00000000-0000-0000-0000-00000000000a"
        sess["anvil_admin_iat"] = time.time()
        if with_token:
            sess["anvil_csrf"] = token


def _fake_cursor():
    cur = MagicMock()
    cur.rowcount = 1
    cm = MagicMock()
    cm.__enter__.return_value = cur
    cm.__exit__.return_value = False
    return cm, cur


class TestSupportUpdate:
    URL = f"/admin/support/{SUPPORT_ID}"

    def test_rejects_missing_token_and_writes_nothing(self, client):
        _admin_session(client)
        cm, cur = _fake_cursor()
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("supabase_client.cursor", return_value=cm):
            r = client.post(self.URL, json={"status": "closed"})
        assert r.status_code == 403
        assert r.get_json()["ok"] is False
        cur.execute.assert_not_called()

    def test_rejects_wrong_token(self, client):
        _admin_session(client)
        cm, cur = _fake_cursor()
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("supabase_client.cursor", return_value=cm):
            r = client.post(self.URL, json={"status": "closed"},
                            headers={"X-CSRF-Token": "wrong"})
        assert r.status_code == 403
        cur.execute.assert_not_called()

    def test_rejects_when_session_has_no_token(self, client):
        """A header alone proves nothing: the session must hold the token."""
        _admin_session(client, with_token=False)
        cm, cur = _fake_cursor()
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("supabase_client.cursor", return_value=cm):
            r = client.post(self.URL, json={"status": "closed"},
                            headers={"X-CSRF-Token": TOKEN})
        assert r.status_code == 403
        cur.execute.assert_not_called()

    def test_accepts_correct_token(self, client):
        _admin_session(client)
        cm, cur = _fake_cursor()
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("supabase_client.cursor", return_value=cm):
            r = client.post(self.URL, json={"status": "closed"},
                            headers={"X-CSRF-Token": TOKEN})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        cur.execute.assert_called_once()

    def test_unauthenticated_gets_401_before_csrf(self, client):
        """Auth is checked before CSRF, so anonymous JSON callers get 401, not 403."""
        r = client.post(self.URL, json={"status": "closed"})
        assert r.status_code == 401

    def test_unauthenticated_html_is_redirected_to_login(self, client):
        r = client.post(self.URL, data={"status": "closed"})
        assert r.status_code == 302
        assert "/admin/login" in r.headers["Location"]


class TestQueueRetry:
    URL = "/admin/queue/1/retry"

    def test_form_post_without_token_is_rejected(self, client):
        _admin_session(client)
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("routes.admin.retry_dead_letter", return_value=True) as retry:
            r = client.post(self.URL)
        assert r.status_code == 403
        retry.assert_not_called()

    def test_form_field_token_is_accepted(self, client):
        """The HTML form submits the token as a hidden field."""
        _admin_session(client)
        with patch("auth.admin.is_user_admin", return_value=True), \
             patch("routes.admin.retry_dead_letter", return_value=True) as retry:
            r = client.post(self.URL, data={"csrf_token": TOKEN})
        assert r.status_code == 200
        retry.assert_called_once_with(1)


class TestLogoutAndLogin:
    def test_logout_without_token_keeps_session_alive(self, client):
        """A cross-site logout must not sign the admin out."""
        _admin_session(client)
        with patch("auth.admin.is_user_admin", return_value=True):
            r = client.post("/admin/logout")
            assert r.status_code == 403
            # Session still valid: the admin dashboard still renders.
            r2 = client.get("/admin/")
        assert r2.status_code == 200

    def test_login_without_token_creates_no_session(self, client):
        fake_user = MagicMock(user_id="00000000-0000-0000-0000-00000000000b",
                              email="a@example.com")
        with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
             patch("auth.admin.is_user_admin", return_value=True):
            r = client.post("/admin/login", json={"access_token": "valid"})
        assert r.status_code == 403
        with client.session_transaction() as sess:
            assert "anvil_admin_uid" not in sess


class TestTokenWiring:
    def test_admin_pages_embed_token_meta_tag(self, client):
        _admin_session(client)
        with patch("auth.admin.is_user_admin", return_value=True):
            r = client.get("/admin/")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert 'name="csrf-token" content="' in body
        assert TOKEN in body

    def test_logout_form_carries_hidden_token(self, client):
        _admin_session(client)
        with patch("auth.admin.is_user_admin", return_value=True):
            body = client.get("/admin/").get_data(as_text=True)
        assert 'name="csrf_token" value="' + TOKEN + '"' in body

    def test_public_pages_do_not_create_a_session_token(self, client):
        """Anonymous visitors must not receive a session cookie from CSRF setup."""
        r = client.get("/")
        assert r.status_code == 200
        assert "Set-Cookie" not in r.headers or "session=" not in r.headers.get("Set-Cookie", "")

    def test_token_is_random_per_session(self, client):
        from auth import admin as a
        with client.application.test_request_context("/"):
            from flask import session
            session.clear()
            t1 = a.csrf_token()
            session.clear()
            t2 = a.csrf_token()
        assert t1 and t2 and t1 != t2
        assert len(t1) >= 43
