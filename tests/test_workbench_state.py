"""
Server-side checks for Workbench state preservation (runs without a browser).

Covers the wiring the browser tests depend on: the draft script is served and
loaded before workbench.js, the draft UI exists, the service worker precaches
the new script, and draft storage stays in sessionStorage (never localStorage
or the server).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_workbench_page_loads_draft_script_before_workbench(client):
    html = client.get("/workbench").get_data(as_text=True)
    drafts_at = html.find("js/workbench-drafts.js")
    workbench_at = html.find("js/workbench.js")
    assert drafts_at != -1, "workbench-drafts.js is not included"
    assert workbench_at != -1
    assert drafts_at < workbench_at, "draft store must load before workbench.js"


def test_workbench_page_has_draft_notice_and_clear_control(client):
    html = client.get("/workbench").get_data(as_text=True)
    assert 'id="wb-draft-note"' in html
    assert 'id="wb-clear-drafts"' in html


def test_draft_script_is_served(client):
    resp = client.get("/static/js/workbench-drafts.js")
    assert resp.status_code == 200
    assert b"createDraftStore" in resp.data


def test_service_worker_precaches_draft_script_and_bumps_cache():
    sw = read("static", "js", "service-worker.js")
    assert '"/static/js/workbench-drafts.js"' in sw
    # Old cache names must not survive: a new version forces a fresh shell.
    match = re.search(r'CACHE_NAME = "anvil-cache-v(\d+)"', sw)
    assert match and int(match.group(1)) >= 8


def test_drafts_use_session_storage_only():
    """Privacy guard: drafts must never reach localStorage, cookies or the server."""
    for name in ("workbench.js", "workbench-drafts.js"):
        source = read("static", "js", name)
        assert "localStorage" not in source, f"{name} must not use localStorage"
        assert "document.cookie" not in source, f"{name} must not set cookies"
    drafts = read("static", "js", "workbench-drafts.js")
    assert "sessionStorage" in drafts


def test_jwt_slot_excluded_from_persistence_in_source():
    drafts = read("static", "js", "workbench-drafts.js")
    assert re.search(r'NEVER_PERSIST\s*=\s*\[\s*"jwt"\s*\]', drafts)


def test_workbench_still_renders_for_every_operation(client):
    for query in ("", "?operation=converter&format=json-xml", "?operation=diff&format=text",
                  "?operation=decoder&format=jwt", "?operation=generator&format=hash"):
        assert client.get("/workbench" + query).status_code == 200, query
