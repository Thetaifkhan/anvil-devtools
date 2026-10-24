"""
Browser regression tests for Workbench input-state preservation.

These drive the real page in headless Chromium through Playwright. They are
skipped automatically when Playwright or a Chromium build is not installed, so
the default `pytest` run in environments without a browser stays green.

Set ANVIL_CHROMIUM=/path/to/chromium to point at a specific binary.
"""
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="Playwright is not installed"
).sync_playwright

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402

DRAFTS_KEY = "anvil.workbench.drafts"
XML = "<root><item>1</item></root>"


@pytest.fixture(scope="module")
def base_url():
    app = create_app()
    app.config.update(TESTING=True)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        launch_kwargs = {}
        if os.environ.get("ANVIL_CHROMIUM"):
            launch_kwargs["executable_path"] = os.environ["ANVIL_CHROMIUM"]
        try:
            b = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001 - any launch failure means no browser
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, base_url):
    """A fresh context per test, so sessionStorage never leaks between tests."""
    context = browser.new_context()
    pg = context.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.goto(f"{base_url}/workbench")
    yield pg
    context.close()


# ── helpers ──────────────────────────────────────────────────────────────────

def value(pg, element_id):
    return pg.eval_on_selector(f"#{element_id}", "el => el.value")


def text(pg, element_id):
    return pg.eval_on_selector(f"#{element_id}", "el => el.textContent")


def status(pg):
    return pg.text_content("#wb-status")


def select(pg, selector_id, option):
    pg.select_option(f"#{selector_id}", option)


def stored_drafts(pg):
    raw = pg.evaluate(f"sessionStorage.getItem('{DRAFTS_KEY}')")
    return json.loads(raw) if raw else None


def wait_until(pg, predicate, timeout_ms=5000, what="condition"):
    """Polls a Python predicate. Avoids the string form of Playwright's
    wait-for-function, which is evaluated as JS and blocked by the app's CSP."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        pg.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {what}")


def wait_for_text(pg, element_id, needle):
    wait_until(pg, lambda: needle in text(pg, element_id), what=f"{needle!r} in #{element_id}")


def settle(pg):
    # Drafts persist after a 250 ms debounce; wait it out before reloading.
    pg.wait_for_timeout(400)


def fulfil_json(route, payload):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def hold(store):
    """Route handler that parks requests so the test decides when they reply."""
    def handler(route, request):
        store.append(route)
    return handler


# ── Format and operation switches never clear input ──────────────────────────

def test_xml_pasted_then_switch_to_json_keeps_original_text(page):
    page.fill("#wb-input", XML)
    select(page, "format-select", "xml")
    assert value(page, "wb-input") == XML
    select(page, "format-select", "json")
    assert value(page, "wb-input") == XML


def test_switching_operation_keeps_text_and_returning_restores_it(page):
    page.fill("#wb-input", XML)
    select(page, "op-select", "validator")
    assert value(page, "wb-input") == XML
    select(page, "op-select", "formatter")
    assert value(page, "wb-input") == XML


def test_changing_options_does_not_reset_input(page):
    page.fill("#wb-input", '{"b":1,"a":2}')
    select(page, "format-select", "json")
    page.check("#opt-sort")
    page.select_option("#opt-indent", "4")
    assert value(page, "wb-input") == '{"b":1,"a":2}'


def test_draft_is_preserved_per_operation_when_switching_back(page):
    page.fill("#wb-input", "formatter draft")
    select(page, "op-select", "validator")
    page.fill("#wb-input", "validator draft")
    select(page, "op-select", "formatter")
    assert value(page, "wb-input") == "formatter draft"
    select(page, "op-select", "validator")
    assert value(page, "wb-input") == "validator draft"


# ── Errors and failed requests keep the user's input ─────────────────────────

def test_malformed_json_error_keeps_input(page):
    bad = '{"a": '
    page.fill("#wb-input", bad)
    page.click("#run-btn")
    assert "Invalid JSON" in text(page, "wb-output")
    assert value(page, "wb-input") == bad


def test_failed_api_request_keeps_input(page):
    page.route("**/api/xml-format", lambda route, req: route.abort())
    select(page, "format-select", "xml")
    page.fill("#wb-input", XML)
    page.click("#run-btn")
    wait_for_text(page, "wb-status", "Network error")
    assert value(page, "wb-input") == XML


def test_output_and_input_stay_independent(page):
    page.fill("#wb-input", '{"a":1}')
    page.click("#run-btn")
    assert value(page, "wb-input") == '{"a":1}'
    assert text(page, "wb-output").strip() == '{\n  "a": 1\n}'


# ── Stale asynchronous responses ─────────────────────────────────────────────

def test_response_is_dropped_when_input_is_edited_while_running(page):
    held = []
    page.route("**/api/xml-format", hold(held))
    select(page, "format-select", "xml")
    page.fill("#wb-input", "<a/>")
    page.click("button:has-text('Beautify')")
    page.wait_for_timeout(200)
    assert len(held) == 1
    page.fill("#wb-input", "<b/>")
    fulfil_json(held[0], {"ok": True, "result": "<a/>\n"})
    page.wait_for_timeout(200)
    assert value(page, "wb-input") == "<b/>"
    assert "<a/>" not in text(page, "wb-output")
    assert "Input changed" in status(page)


def test_response_is_dropped_after_switching_format(page):
    held = []
    page.route("**/api/xml-format", hold(held))
    select(page, "format-select", "xml")
    page.fill("#wb-input", "<a/>")
    page.click("button:has-text('Beautify')")
    page.wait_for_timeout(200)
    select(page, "format-select", "json")
    fulfil_json(held[0], {"ok": True, "result": "<a/>\n"})
    page.wait_for_timeout(200)
    assert "<a/>" not in text(page, "wb-output")
    assert value(page, "wb-input") == "<a/>"


def test_older_response_cannot_overwrite_newer_result(page):
    held = []
    page.route("**/api/xml-format", hold(held))
    select(page, "format-select", "xml")
    page.fill("#wb-input", "<a/>")
    page.click("button:has-text('Beautify')")
    page.wait_for_timeout(150)
    page.keyboard.press("Control+Enter")   # second run, same input
    page.wait_for_timeout(150)
    assert len(held) == 2
    fulfil_json(held[1], {"ok": True, "result": "NEWER"})
    page.wait_for_timeout(150)
    fulfil_json(held[0], {"ok": True, "result": "OLDER"})
    page.wait_for_timeout(150)
    assert text(page, "wb-output") == "NEWER"


# ── Output is tied to its own view ───────────────────────────────────────────

def test_output_is_restored_per_view(page):
    page.fill("#wb-input", '{"a":1}')
    page.click("#run-btn")
    formatted = text(page, "wb-output")
    select(page, "format-select", "xml")
    assert text(page, "wb-output") == ""
    select(page, "format-select", "json")
    assert text(page, "wb-output") == formatted


# ── Clear actions ────────────────────────────────────────────────────────────

def test_clear_only_clears_the_current_operation(page):
    page.fill("#wb-input", "keep me")
    select(page, "op-select", "validator")
    page.click("#wb-clear")
    assert value(page, "wb-input") == ""
    select(page, "op-select", "formatter")
    assert value(page, "wb-input") == "keep me"


def test_switching_format_never_triggers_clear(page):
    page.fill("#wb-input", XML)
    page.click("#run-btn")
    select(page, "format-select", "yaml")
    assert value(page, "wb-input") == XML
    select(page, "format-select", "sql")
    assert value(page, "wb-input") == XML
    select(page, "format-select", "json")
    assert value(page, "wb-input") == XML
    assert "Clear" not in status(page)


def test_clear_keeps_selected_options(page):
    select(page, "format-select", "json")
    page.wait_for_timeout(100)   # option values are re-applied on the next frame after a switch
    page.select_option("#opt-indent", "4")
    page.fill("#wb-input", "x")
    page.click("#wb-clear")
    assert page.input_value("#wb-input") == ""
    assert page.eval_on_selector("#opt-indent", "el => el.value") == "4"


# ── Separate inputs (diff) and JWT slot ──────────────────────────────────────

def test_diff_inputs_stay_independent_and_survive_switching(page):
    select(page, "op-select", "diff")
    page.fill("#wb-input-a", '{"x":1}')
    page.fill("#wb-input-b", '{"x":2}')
    select(page, "op-select", "formatter")
    select(page, "op-select", "diff")
    assert value(page, "wb-input-a") == '{"x":1}'
    assert value(page, "wb-input-b") == '{"x":2}'


def test_jwt_is_never_written_to_storage(page):
    select(page, "op-select", "decoder")
    select(page, "format-select", "jwt")
    page.fill("#wb-jwt-input", "eyJhbGciOiJub25lIn0.eyJzdWIiOiIxIn0.")
    settle(page)
    stored = stored_drafts(page)
    assert stored is None or "jwt" not in stored.get("drafts", {})


# ── Refresh recovery and the saved-drafts policy ─────────────────────────────

def test_refresh_restores_input_draft(page, base_url):
    page.fill("#wb-input", '{"keep": true}')
    settle(page)
    page.reload()
    assert value(page, "wb-input") == '{"keep": true}'


def test_clear_saved_drafts_removes_storage_and_inputs(page):
    page.fill("#wb-input", "remove me")
    settle(page)
    page.click("#wb-clear-drafts")
    assert value(page, "wb-input") == ""
    assert stored_drafts(page) is None


def test_different_operations_do_not_overwrite_each_others_stored_drafts(page):
    page.fill("#wb-input", "formatter text")
    select(page, "op-select", "converter")
    page.fill("#wb-input", "converter text")
    settle(page)
    drafts = stored_drafts(page)["drafts"]
    assert drafts["formatter"]["a"] == "formatter text"
    assert drafts["converter"]["a"] == "converter text"


def test_malformed_stored_state_does_not_break_workbench(page, base_url):
    page.evaluate(f"sessionStorage.setItem('{DRAFTS_KEY}', '{{not json')")
    page.reload()
    assert not page.errors
    page.fill("#wb-input", '{"a":1}')
    page.click("#run-btn")
    assert "\"a\": 1" in text(page, "wb-output")
    assert not page.errors


def test_outdated_schema_version_is_ignored(page):
    page.evaluate(
        f"sessionStorage.setItem('{DRAFTS_KEY}', JSON.stringify({{v: 99, drafts: {{formatter: {{a: 'old', b: ''}}}}}}))"
    )
    page.reload()
    assert value(page, "wb-input") == ""
    assert not page.errors


def test_blocked_storage_keeps_workbench_usable(browser, base_url):
    context = browser.new_context()
    context.add_init_script(
        "Object.defineProperty(window, 'sessionStorage', {get() {"
        " throw new DOMException('blocked', 'SecurityError'); }});"
    )
    pg = context.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.goto(f"{base_url}/workbench")
    assert "unavailable" in pg.text_content("#wb-draft-note")
    pg.fill("#wb-input", XML)
    select(pg, "format-select", "xml")
    assert value(pg, "wb-input") == XML
    assert not pg.errors
    context.close()


def test_draft_note_explains_persistence(page):
    assert "until you close it" in page.text_content("#wb-draft-note")


# ── Rewritten async paths: YAML (two-step) and hash generator (awaits crypto) ─

def test_yaml_format_success_through_live_server(page):
    select(page, "format-select", "yaml")
    page.fill("#wb-input", "a:   1")
    page.click("#run-btn")
    wait_for_text(page, "wb-output", "a: 1")
    assert value(page, "wb-input") == "a:   1"


def test_yaml_validate_error_keeps_input(page):
    select(page, "op-select", "validator")
    select(page, "format-select", "yaml")
    bad = "a: [unclosed"
    page.fill("#wb-input", bad)
    page.click("#run-btn")
    wait_until(page, lambda: "has-error" in page.eval_on_selector("#wb-output", "el => el.className"), what="error state")
    assert value(page, "wb-input") == bad


def test_hash_generator_output_and_input(page):
    select(page, "op-select", "generator")
    select(page, "format-select", "hash")
    page.fill("#wb-input", "abc")
    page.click("#run-btn")
    wait_for_text(page, "wb-output", "SHA-256")
    # MD5("abc") is a well-known test vector.
    assert "900150983cd24fb0d6963f7d28e17f72" in text(page, "wb-output")
    assert value(page, "wb-input") == "abc"
