"""Phase 2 §2.4 admin dashboard — tests.

Covers: the overview status banner logic (green/amber/red + one sentence),
the three overview questions, the new /admin/health page and its data, and
the admin nav gaining a Health link.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

CSRF_TEST_TOKEN = "h" * 43


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
    import supabase_client
    supabase_client.reset_engine()
    supabase_client.reset_health()
    yield
    supabase_client.reset_engine()
    supabase_client.reset_health()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


def _login(client):
    fake_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000ba",
                          email="admin@example.com")
    with patch("auth.admin.verify_supabase_token", return_value=fake_user), \
         patch("auth.admin.is_user_admin", return_value=True):
        client.post("/admin/login", json={"access_token": "t"},
                    headers=_csrf(client))


@contextmanager
def _admin_ctx(client):
    with patch("auth.admin.is_user_admin", return_value=True):
        yield


# ── Banner logic (unit) ────────────────────────────────────────────────────

def test_banner_green_when_all_good():
    from routes.admin import _overview_banner
    b = _overview_banner(configured=True, reachable=True, q={"pending": 0})
    assert b["level"] == "green"
    assert b["message"]


def test_banner_amber_when_waiting():
    from routes.admin import _overview_banner
    # "Waiting" counts USER-FACING items (contacts + analytics), not
    # internal admin-audit events.
    b = _overview_banner(configured=True, reachable=True,
                         q={"pending": 7, "retrying": 2, "dead_lettered": 0,
                            "contact_pending": 5, "analytics_pending": 2})
    assert b["level"] == "amber"
    assert "7" in b["message"] and "no action needed" in b["message"]

    # Only internal audit events queued -> stays green.
    b = _overview_banner(configured=True, reachable=True,
                         q={"pending": 3, "dead_lettered": 0,
                            "contact_pending": 0, "analytics_pending": 0})
    assert b["level"] == "green"


def test_banner_red_when_dead_lettered():
    from routes.admin import _overview_banner
    b = _overview_banner(configured=True, reachable=True,
                         q={"pending": 0, "dead_lettered": 3})
    assert b["level"] == "red"
    assert "Retry all failed" in b["message"]


def test_banner_red_when_db_down():
    from routes.admin import _overview_banner
    b = _overview_banner(configured=True, reachable=False, q={"pending": 0})
    assert b["level"] == "red"
    assert "not connected" in b["message"]


def test_banner_amber_when_not_configured():
    from routes.admin import _overview_banner
    b = _overview_banner(configured=False, reachable=False, q={})
    assert b["level"] == "amber"


# ── Overview page ──────────────────────────────────────────────────────────

def test_overview_renders_banner_and_three_questions(client):
    import supabase_client
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=True), \
         patch.object(supabase_client, "cursor", _fake_kpi_cursor()):
        r = client.get("/admin/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "admin-banner green" in html
    for question in ("Is the database connected?", "Is anything stuck?",
                     "What happened today?"):
        assert question in html, f"overview must answer: {question}"


def test_overview_banner_is_red_with_action_when_db_down(client):
    import supabase_client

    @contextmanager
    def dead_cursor():
        raise ConnectionError("down")
        yield  # pragma: no cover

    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=False), \
         patch.object(supabase_client, "cursor", dead_cursor):
        r = client.get("/admin/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "admin-banner red" in html
    assert "kept on this server" in html
    assert "Not connected" in html


def test_overview_shows_retry_action_for_dead_letters(client):
    import supabase_client
    outbox.enqueue("analytics_event", {"x": 1}, event_key="b-1")
    item = outbox.claim_pending()[0]
    outbox.dead_letter(item.id, reason="Invalid data format")
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=True):
        r = client.get("/admin/")
    html = r.get_data(as_text=True)
    assert "Retry all failed" in html
    assert "admin-banner red" in html


def _fake_kpi_cursor():
    from contextlib import contextmanager

    @contextmanager
    def fake_cursor():
        class Cur:
            def execute(self, sql, params=()):
                pass

            def fetchone(self):
                return (12, 5, 5, 2, 1, 0)

            def fetchall(self):
                return []
        yield Cur()
    return fake_cursor


# ── Health page ────────────────────────────────────────────────────────────

def test_health_page_requires_admin(client):
    r = client.get("/admin/health")
    assert r.status_code == 302


def test_health_page_lists_all_required_facts(client):
    import supabase_client
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=True):
        r = client.get("/admin/health")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for needle in ("Database", "Delivery worker", "Local queue", "Server",
                   "Last successful write", "Last run", "Disk free",
                   "Version", "File size"):
        assert needle in html, f"health page must show: {needle}"


def test_health_page_shows_version_from_env(client, monkeypatch):
    import supabase_client
    monkeypatch.setenv("GIT_COMMIT_SHA", "abc1234")
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=True):
        r = client.get("/admin/health")
    assert "abc1234" in r.get_data(as_text=True)


def test_health_page_survives_missing_disk_info(client, monkeypatch):
    import supabase_client

    def broken_usage(_):
        raise OSError("no disk")

    monkeypatch.setattr("shutil.disk_usage", broken_usage)
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=False):
        r = client.get("/admin/health")
    assert r.status_code == 200
    assert "unknown" in r.get_data(as_text=True)


# ── Nav + templates ────────────────────────────────────────────────────────

def test_admin_nav_has_health_link(client):
    import supabase_client
    _login(client)
    with _admin_ctx(client), \
         patch.object(supabase_client, "is_configured", return_value=True), \
         patch.object(supabase_client, "ping", return_value=True), \
         patch.object(supabase_client, "cursor", _fake_kpi_cursor()):
        r = client.get("/admin/")
    assert "/admin/health" in r.get_data(as_text=True)


def test_admin_tables_are_enhanceable(client):
    """The queue/support/audit tables opt into sorting + search."""
    from pathlib import Path
    for name in ("queue.html", "support_inbox.html", "audit.html"):
        src = (Path("templates/admin") / name).read_text(encoding="utf-8")
        assert "data-enhance" in src, f"{name} must opt into the enhancer"
        assert "time data-ago" in src or "data-ago" in src, (
            f"{name} must use human-readable times")


def test_admin_js_contains_sort_search_and_timeago():
    from pathlib import Path
    js = Path("static/js/admin.js").read_text(encoding="utf-8")
    assert "aria-sort" in js and "table-search" in js
    assert "timeAgo" in js and "minutes ago" in js
    # Times refresh so "3 minutes ago" stays honest.
    assert "setInterval" in js
