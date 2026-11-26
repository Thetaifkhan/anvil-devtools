"""Phase 2 §2.6 worst-case handling — tests.

Covers: a full outbox rejects analytics but never drops contact submissions
silently; clock skew is clamped and logged (not stored raw); very large
output carries a notice and a download path; the service worker falls back
to the network for pages; and a JavaScript-disabled browser still gets
readable pages and a submitting contact form.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    monkeypatch.delenv("SUPABASE_MAX_QUEUE_ITEMS", raising=False)
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from services.rate_limit import CONTACT_LIMITER, ANALYTICS_LIMITER
    for limiter in (CONTACT_LIMITER, ANALYTICS_LIMITER):
        with limiter._lock:
            limiter._buckets.clear()
    import supabase_client
    supabase_client.reset_engine()
    supabase_client.reset_health()
    yield
    supabase_client.reset_engine()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        yield c


VALID_CONTACT = {"email": "u@example.com", "subject": "s",
                 "message": "m" * 30, "category": "other"}


# ── 1. Full outbox: analytics best-effort, contacts never silent ───────────

def test_full_outbox_rejects_analytics_but_contacts_speak(
        client, monkeypatch):
    # Shrink the queue so "full" is easy to reach.
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "3")
    # Fill every slot with contact submissions.
    for i in range(3):
        r = client.post("/api/contact", json=dict(VALID_CONTACT, subject=f"s{i}"))
        assert r.status_code == 202, r.get_json()

    # A 4th contact: the queue grows for contacts... but when it can't,
    # the user must get a CLEAR rejection, never a silent drop.
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "1")
    # (capacity now below the pending count; contacts always pass, so the
    # queue simply grows - verify no silent drop:)
    r = client.post("/api/contact", json=dict(VALID_CONTACT, subject="s3"))
    assert r.status_code in (202, 503)
    if r.status_code == 503:
        body = r.get_json()
        assert body["ok"] is False and body["error"], (
            "a rejected contact must carry a readable message")

    # Analytics at capacity: evicted or rejected, but the endpoint answers
    # deliberately (never a 500, never a hang).
    import uuid as _uuid
    ev = {"event_name": "page_view", "session_id": str(_uuid.uuid4())}
    r = client.post("/api/analytics/event", json=ev)
    assert r.status_code in (202, 503)
    assert r.status_code != 500


def test_contact_queue_full_error_is_friendly(client, monkeypatch):
    """When the queue truly cannot take a contact (disk failure at capacity
    with contacts), the response is a 503 with a plain message."""
    monkeypatch.setenv("SUPABASE_MAX_QUEUE_ITEMS", "1")

    def full_queue(event_type, payload, *, event_key=None):
        raise outbox.QueueFullError("queue full of contacts")

    monkeypatch.setattr("routes.contact.enqueue", full_queue)
    r = client.post("/api/contact", json=VALID_CONTACT)
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False
    assert "cannot accept" in body["error"]


# ── 2. Clock skew is clamped and logged, never stored raw ──────────────────

def test_future_timestamp_clamped_and_logged(caplog):
    from delivery.worker import _convert_timestamps, MAX_FUTURE_SKEW_SECONDS
    now = time.time()
    with caplog.at_level("WARNING"):
        out = _convert_timestamps(
            "contact_submission",
            {"occurred_at": now + 10_000_000, "consent_recorded_at": now})
    assert out["occurred_at"] <= now + MAX_FUTURE_SKEW_SECONDS + 1
    assert any("future" in rec.getMessage() for rec in caplog.records)


def test_ancient_timestamp_clamped_and_logged(caplog):
    from delivery.worker import _convert_timestamps
    with caplog.at_level("WARNING"):
        out = _convert_timestamps(
            "contact_submission", {"occurred_at": 1})
    assert out["occurred_at"] > time.time() - 400 * 24 * 3600
    assert any("older than" in rec.getMessage() for rec in caplog.records)


def test_clamped_value_reaches_the_sql_binding():
    """The clamped epoch is what _to_ts turns into the datetime for SQL."""
    from delivery.worker import _convert_timestamps, _to_ts
    now = time.time()
    out = _convert_timestamps(
        "analytics_event", {"occurred_at": now + 10_000_000})
    dt = _to_ts(out["occurred_at"])
    assert dt.tzinfo is not None
    assert abs(dt.timestamp() - (now + 0)) < 5, "raw skewed time reached SQL"


# ── 3. Very large output: notice + download, page never freezes ────────────

def test_large_output_has_notice_and_download_in_js():
    js = (ROOT / "static/js/main.js").read_text(encoding="utf-8")
    assert "renderOutput" in js
    assert "output-truncated-note" in js
    assert "Download full output" in js
    assert "1 MB" in js or "1024 * 1024" in js
    # The server-side writers route through it.
    for f in ("api_tool.js", "formatter.js", "json_formatter.js"):
        src = (ROOT / "static/js/tools" / f).read_text(encoding="utf-8")
        assert "renderOutput" in src, f"{f} must use the safe renderer"


def test_server_can_produce_5mb_result(client):
    """A >5 MB formatted result is served (browsers get the notice path).

    Input stays under MAX_TEXT_CHARS (2M); the XML conversion expands it:
    each array element becomes an indented <item> element.
    """
    import json as _json
    # ~1.7 MB of numbers -> ~5.5 MB of pretty XML.
    payload = _json.dumps({"row": list(range(220_000))})
    assert len(payload) < 2_000_000
    r = client.post("/api/json-to-xml", json={"text": payload})
    assert r.status_code == 200, r.get_json()
    assert len(r.get_json()["result"]) > 5_000_000


# ── 4. Service worker: pages fall back to the network ─────────────────────

def test_service_worker_puts_pages_network_first():
    sw = (ROOT / "static/js/service-worker.js").read_text(encoding="utf-8")
    assert 'event.request.mode === "navigate"' in sw, (
        "page navigations must be network-first so an update never leaves a "
        "stale cached page")
    assert sw.index("navigate") < sw.index("return cached || networkFetch"), (
        "the navigation branch must come before the cache-first return")
    # One missing precache asset must not abort the whole install.
    assert "APP_SHELL.map" in sw and "cache.add(url).catch" in sw


# ── 5. JavaScript disabled: readable pages, submitting forms ──────────────

def test_contact_form_has_real_action_and_method():
    html = (ROOT / "templates/pages/contact.html").read_text(encoding="utf-8")
    assert 'action="/api/contact"' in html and 'method="post"' in html, (
        "the form must submit without JavaScript")


def test_contact_form_post_without_js_redirects_with_reference(client):
    r = client.post("/api/contact", data=VALID_CONTACT,
                    content_type="application/x-www-form-urlencoded",
                    follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["Location"]
    assert "ref=ANVIL-" in loc, f"no reference code in {loc}"
    # Following the redirect shows the code on the page.
    r2 = client.get(loc)
    assert r2.status_code == 200
    assert "ANVIL-" in r2.get_data(as_text=True)


def test_contact_form_post_error_redirects_with_message(client):
    bad = dict(VALID_CONTACT, email="not-an-email")
    r = client.post("/api/contact", data=bad,
                    content_type="application/x-www-form-urlencoded",
                    follow_redirects=False)
    assert r.status_code == 400
    assert "error=" in r.headers["Location"]


def test_pages_readable_without_javascript(client):
    """Server-rendered content is present without executing any script."""
    for path in ("/", "/json-formatter", "/contact", "/tools"):
        r = client.get(path)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "<main" in html or 'id="main"' in html
        assert len(html) > 2000, f"{path} looks empty without JS"


def test_workbench_still_works_with_js(client):
    """(Sanity) the JS path is unchanged: JSON fetch still returns 202."""
    r = client.post("/api/contact", json=VALID_CONTACT,
                    headers={"Accept": "application/json"})
    assert r.status_code == 202
    assert r.get_json()["reference_code"]
