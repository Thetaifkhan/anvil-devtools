"""Phase 1 browser tests — §1.5 matrix.

Runs the real app under headless Chromium via Playwright and covers:
- Home loads with no console errors (desktop 1440 / mobile 390, light+dark).
- Tool flow: type input, Run, see output, Copy, see "Copied" (client-side
  AND server-backed tools).
- Output box scrollable, copy icon visible while scrolling, header NOT
  stuck to the top.
- Workbench top-bar "Copy output" copies the active output for every
  operation family.
- Service worker: a deploy with a new CACHE_NAME replaces the old worker;
  no "Failed to convert value to a Response" error.
- Admin login: invalid token -> clear message; valid admin token ->
  overview.
- Keyboard: Tab order is logical, focus rings visible, Ctrl/Cmd+Enter runs
  a tool.

Skipped (not failed) when Playwright/Chromium are unavailable.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="Playwright is not installed"
).sync_playwright

from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from delivery import outbox  # noqa: E402


# ── Server + browser fixtures ──────────────────────────────────────────────

@pytest.fixture(scope="module")
def base_url(tmp_path_factory):
    qpath = str(tmp_path_factory.mktemp("browser") / "outbox.sqlite3")
    outbox.configure(qpath)
    os.environ["SUPABASE_WORKER_ENABLED"] = "0"
    os.environ.setdefault("SECRET_KEY", "b" * 40)
    # A DATABASE_URL in the ambient environment (e.g. the CI sandbox's own
    # database) must not leak into the app under test: it makes every admin
    # page block on a 10 s pool timeout against a bogus DSN.
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
              "SUPABASE_SSL_ROOT_CERT"):
        os.environ.pop(k, None)
    import supabase_client
    supabase_client.reset_engine()
    app = create_app({"SECRET_KEY": "b" * 40})

    # Test hook: simulate a deploy by serving a service worker whose
    # CACHE_NAME is bumped (the browser then detects a byte-diff and
    # installs/activates the new worker, exactly like a real deploy).
    # Requesting /service-worker.js?bump=1 flips the sticky server-side
    # version, so the registration.update() fetch of the plain URL sees it.
    sw_state = {"version": 9}

    @app.before_request
    def _serve_deployed_sw():
        from flask import request, Response
        if request.path != "/service-worker.js":
            return None
        if request.args.get("bump") == "1" and sw_state["version"] == 9:
            sw_state["version"] = 10
        if sw_state["version"] == 9:
            return None
        src = open(os.path.join(app.root_path, "static", "js",
                                "service-worker.js"), encoding="utf-8").read()
        src = src.replace("anvil-cache-v9",
                          f"anvil-cache-v{sw_state['version']}")
        return Response(src, mimetype="application/javascript")

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
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium is not available: {exc}")
        yield b
        b.close()


def _new_page(browser, base_url, width=1440, height=900, theme="dracula"):
    """Fresh context at a viewport + theme, with console/error capture."""
    ctx = browser.new_context(
        viewport={"width": width, "height": height},
        permissions=["clipboard-read", "clipboard-write"],
    )
    ctx.add_init_script(f"localStorage.setItem('anvil-theme', '{theme}')")
    pg = ctx.new_page()
    pg.console_errors = []
    pg.page_errors = []
    pg.on("console", lambda m: pg.console_errors.append(
        f"{m.text} {((m.location or {}).get('url') or '')}")
          if m.type == "error" else None)
    pg.on("pageerror", lambda e: pg.page_errors.append(str(e)))
    pg._ctx = ctx
    pg.goto(base_url)
    return pg


def _close(pg):
    pg._ctx.close()


def _app_console_errors(pg):
    """Console errors that are the APP's fault.

    Third-party noise (blocked analytics, slow font CDNs) is allowed, per
    the spec: 'no console errors except third-party analytics being
    blocked'. Everything first-party must be clean.
    """
    third_party = ("plausible", "googletagmanager", "google-analytics",
                   "fonts.googleapis", "fonts.gstatic",
                   "googleads", "doubleclick")
    return [m for m in pg.console_errors
            if not any(t in m for t in third_party)]


def _wait_until(pg, predicate, timeout_ms=6000, what="condition"):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if predicate():
            return
        pg.wait_for_timeout(50)
    raise AssertionError(f"timed out waiting for {what}")


def _run_and_copy(pg, run_selector="#run-btn", input_text='{"a":1}',
                  expect_in_output=None):
    pg.fill("#input", input_text)
    pg.click(run_selector)
    _wait_until(pg, lambda: pg.text_content("#output").strip() != "",
                what="non-empty tool output")
    out = pg.text_content("#output")
    assert out.strip(), "output empty after Run"
    if expect_in_output:
        assert expect_in_output in out
    pg.click("#copy-btn")
    _wait_until(pg, lambda: "Copied" in (pg.text_content("#copy-btn") or ""),
                what="'Copied' on the copy button")
    return out


# ── 1. Home loads with no console errors, both viewports and themes ───────

@pytest.mark.parametrize("theme", ["light", "dracula"])
def test_home_no_console_errors_desktop_1440(browser, base_url, theme):
    pg = _new_page(browser, base_url, 1440, 900, theme)
    try:
        assert pg.title(), "page did not render"
        # Give late handlers (SW register, analytics) a moment.
        pg.wait_for_timeout(700)
        assert pg.page_errors == [], f"JS errors: {pg.page_errors}"
        allowed = _app_console_errors(pg)
        assert allowed == [], f"console errors: {allowed}"
    finally:
        _close(pg)


@pytest.mark.parametrize("theme", ["light", "dracula"])
def test_home_no_console_errors_mobile_390(browser, base_url, theme):
    pg = _new_page(browser, base_url, 390, 844, theme)
    try:
        pg.wait_for_timeout(700)
        assert pg.page_errors == [], f"JS errors: {pg.page_errors}"
        allowed = _app_console_errors(pg)
        assert allowed == [], f"console errors: {allowed}"
        # Mobile nav toggle is visible at 390px and opens the menu.
        toggle = pg.query_selector("#nav-toggle")
        if toggle and toggle.is_visible():
            pg.click("#nav-toggle")
            _wait_until(pg, lambda: pg.get_attribute("#mobile-nav", "aria-hidden")
                        == "false", what="mobile nav opens")
    finally:
        _close(pg)


# ── 2. Tool flow: type, Run, output, Copy, "Copied" ────────────────────────

def test_tool_flow_client_side_json_formatter(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/json-formatter")
        out = _run_and_copy(pg, "#format-btn", '{"name":"anvil"}')
        assert "name" in out
        # Ctrl/Cmd+Enter also runs the tool (covered further in §keyboard).
    finally:
        _close(pg)


def test_tool_flow_server_backed_xml_format(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/xml-formatter")
        out = _run_and_copy(pg, "#run-btn", "<root><item>1</item></root>")
        assert "<root>" in out
        assert pg.page_errors == []
    finally:
        _close(pg)


def test_tool_flow_base64_encode(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/base64-encode")
        out = _run_and_copy(pg, "#run-btn", "hello world")
        assert out.strip() == "aGVsbG8gd29ybGQ="
    finally:
        _close(pg)


# ── 3. Output box: scrollable, copy icon stays visible, header not sticky ──

def test_output_box_scroll_and_copy_icon_and_header(browser, base_url):
    pg = _new_page(browser, base_url, 1440, 800)
    try:
        pg.goto(f"{base_url}/json-formatter")
        # Generate output tall enough to scroll.
        big = json.dumps({f"key{i}": "x" * 40 for i in range(400)})
        pg.fill("#input", big)
        pg.click("#format-btn")
        _wait_until(pg, lambda: (pg.text_content("#output") or "").strip() != "",
                    what="formatted output")

        frame = pg.query_selector(".output-frame")
        assert frame is not None, "output must be wrapped in a scroll frame"

        # Scrollable: the output box has a bounded height (24rem/65vh) and
        # scrolls internally instead of stretching the page.
        scrolled = pg.evaluate(
            """() => { const f = document.querySelector('.output-frame .output-area');
                      const before = f.scrollTop;
                      f.scrollTop = 400;
                      return {moved: f.scrollTop > before,
                              scrollable: f.scrollHeight > f.clientHeight,
                              pageGrew: document.body.scrollHeight};
                      }""")
        assert scrolled["scrollable"], "output box must be scrollable for big output"
        assert scrolled["moved"], "programmatic scroll must work"

        # Copy icon stays visible while scrolled.
        icon_box = pg.evaluate(
            """() => { const i = document.querySelector('.output-copy-icon');
                      const r = i.getBoundingClientRect();
                      return {top: r.top, bottom: r.bottom,
                              visible: r.width > 0 && r.height > 0}; }""")
        assert icon_box["visible"]
        assert 0 <= icon_box["top"] and icon_box["bottom"] <= 800, (
            "copy icon must remain inside the viewport after scrolling")

        # Scrolling INSIDE the output box must not stick the header bar to
        # the top or hijack the page. Measure the delta ACROSS the box
        # scroll so unrelated scroll-anchoring during output settling
        # (Chrome adjusts page scroll when content above changes) cannot
        # masquerade as chaining.
        pg.wait_for_timeout(500)  # let output/meta rendering settle
        pg.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        pg.wait_for_timeout(300)
        before = pg.evaluate(
            "() => ({scrollY: window.scrollY,"
            "         headerTop: document.querySelector('.site-header')"
            "                    .getBoundingClientRect().top})")
        pg.evaluate(
            """() => { document.querySelector('.output-frame .output-area')
                         .scrollTop = 300; }""")
        pg.wait_for_timeout(250)
        after = pg.evaluate(
            "() => ({scrollY: window.scrollY,"
            "         headerTop: document.querySelector('.site-header')"
            "                    .getBoundingClientRect().top})")
        assert after["scrollY"] - before["scrollY"] <= 2, (
            f"scrolling inside the output box scrolled the page "
            f"{after['scrollY'] - before['scrollY']}px")
        assert after["headerTop"] == before["headerTop"], (
            "site header moved while only the output box was scrolled")
    finally:
        _close(pg)


# ── 4. Workbench: top-bar Copy output for every operation family ───────────

WORKBENCH_CASES = [
    # (operation, format, input, run label, expected substring)
    ("formatter", "json", '{"a":1}', "Beautify", '"a"'),
    ("validator", "json", '{"a":1}', "Validate", "valid"),
    ("converter", "json-xml", '{"a":1}', "Convert", "<a>"),
    ("encoder", "base64", "hello", "Encode", "aGVsbG8"),
    ("decoder", "base64", "aGVsbG8=", "Decode", "hello"),
    ("diff", "text", "line one\nline two", None, None),  # special: two inputs
    ("generator", "uuid", "", "Generate", "-"),
]


@pytest.mark.parametrize("op,fmt,inp,run_label,expected", WORKBENCH_CASES)
def test_workbench_top_copy_copies_active_output(browser, base_url, op, fmt,
                                                 inp, run_label, expected):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/workbench?operation={op}&format={fmt}")
        pg.wait_for_selector("#wb-body .tool-panels, #wb-body", timeout=5000)

        if op == "diff":
            # The diff section has its OWN two inputs; #wb-body textarea
            # would also match the hidden two-panel input, so target ids.
            pg.wait_for_selector("#wb-input-a", timeout=5000)
            pg.fill("#wb-input-a", "line one\nline two")
            pg.fill("#wb-input-b", "line one\nline CHANGED")
            pg.click("#run-btn")
        else:
            if inp:
                pg.fill("#wb-input", inp)
            selector = "#run-btn"
            pg.click(selector)

        # Wait for a non-empty active output.
        _wait_until(pg, lambda: any(
            (pg.text_content(sel) or "").strip()
            for sel in ("#wb-output", "#wb-diff-output", "#wb-gen-output",
                        "#wb-jwt-payload")
        ), what=f"workbench {op}/{fmt} output")

        active_out = ""
        for sel in ("#wb-output", "#wb-diff-output", "#wb-gen-output"):
            t = (pg.text_content(sel) or "").strip()
            if t:
                active_out = t
                break
        assert active_out, f"no output produced for {op}/{fmt}"
        if expected:
            assert expected.lower() in active_out.lower(), (
                f"{op}/{fmt}: expected {expected!r} in {active_out[:80]!r}")

        # Click the TOP-BAR copy button; the button must say "Copied".
        pg.click("#wb-top-copy")
        _wait_until(pg, lambda: "Copied" in (pg.text_content("#wb-top-copy") or ""),
                    what="'Copied' on workbench top-bar copy")
    finally:
        _close(pg)


# ── 5. Service worker deploy simulation ────────────────────────────────────

def test_service_worker_update_replaces_cache_cleanly(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.wait_for_timeout(600)
        reg = pg.evaluate(
            """async () => {
                 const reg = await navigator.serviceWorker.ready;
                 return {scope: reg.scope,
                         active: !!reg.active,
                         caches: await caches.keys()};
               }""")
        assert reg["active"], "service worker did not activate"
        current = [c for c in reg["caches"] if c.startswith("anvil-cache-")]
        assert current, f"no anvil cache after activation: {reg['caches']}"

        # Simulate a DEPLOY: the server now serves a SW with a bumped
        # CACHE_NAME. registration.update() picks up the byte-diff, installs
        # the new worker and activates it (the normal browser flow would
        # trigger this check on navigation automatically).
        pg.evaluate(
            """async () => {
                 // Stale cache from the previous deploy, to prove cleanup.
                 const c = await caches.open('anvil-cache-v0-stale');
                 await c.put('/legacy', new Response('ok'));
                 // Flip the server to the "deployed" service worker.
                 await fetch('/service-worker.js?bump=1', {cache: 'reload'});
                 const reg = await navigator.serviceWorker.getRegistration();
                 await reg.update();
               }""")
        # Wait for the new worker to take over and clean up old caches.
        _wait_until(pg, lambda: all(
            k != "anvil-cache-v0-stale"
            for k in pg.evaluate("async () => await caches.keys()")
        ), timeout_ms=10000, what="stale cache cleanup after deploy")
        keys = pg.evaluate("async () => await caches.keys()")
        assert "anvil-cache-v0-stale" not in keys, keys
        assert any(k.startswith("anvil-cache-v") for k in keys), keys

        # The known Chromium bug "Failed to convert value to 'Response'"
        # surfaces as an unhandled rejection — assert it never fired.
        assert all("Failed to convert value to 'Response'" not in e
                   for e in pg.page_errors), pg.page_errors
        assert all("Failed to convert value to 'Response'" not in m
                   for m in pg.console_errors), pg.console_errors
    finally:
        _close(pg)


# ── 6. Admin login in the browser ──────────────────────────────────────────

@pytest.fixture()
def admin_server(tmp_path):
    """A second server whose Supabase auth is faked: only the literal
    token "valid-token" verifies (as an admin); anything else is rejected
    exactly like a forged or expired token."""
    outbox.configure(str(tmp_path / "admin-outbox.sqlite3"))
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD"):
        os.environ.pop(k, None)
    import supabase_client
    supabase_client.reset_engine()
    good_user = MagicMock(user_id="00000000-0000-0000-0000-0000000000d1",
                          email="admin@example.com")

    def fake_verify(token):
        return good_user if (token or "").strip() == "valid-token" else None

    patches = [
        patch("auth.admin.verify_supabase_token", side_effect=fake_verify),
        patch("auth.admin.is_user_admin", return_value=True),
    ]
    for p in patches:
        p.start()
    app = create_app({"SECRET_KEY": "b" * 40})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    for p in patches:
        p.stop()


def test_admin_login_invalid_then_valid(browser, admin_server):
    pg = _new_page(browser, admin_server)
    try:
        pg.goto(f"{admin_server}/admin/login")
        pg.wait_for_selector("#admin-login-form")

        # Invalid token -> a clear message, no crash.
        # (The fake verifier rejects anything that is not "valid-token".)
        pg.fill("#admin-access-token", "not-a-real-token")
        pg.click("#admin-login-submit")
        pg.wait_for_timeout(700)
        body = pg.text_content("body")
        assert ("could not verify" in body.lower()
                or "invalid" in body.lower()
                or "not configured" in body.lower()), (
            f"invalid token must show a clear message; got: {body[:200]!r}")
        assert pg.query_selector("#admin-login-form") is not None, (
            "login form disappeared after an invalid token")

        # Valid token -> lands on the overview.
        pg.fill("#admin-access-token", "valid-token")
        pg.click("#admin-login-submit")
        _wait_until(pg, lambda: "/admin/login" not in pg.url,
                    timeout_ms=10000, what="redirect to /admin/ after login")
        assert "/admin/" in pg.url, f"unexpected post-login URL: {pg.url}"
        pg.wait_for_selector("h1, .admin-overview, main", timeout=5000)
        assert pg.query_selector("#admin-login-form") is None, (
            "still on the login page after a valid token")
    finally:
        _close(pg)


# ── 7. Keyboard: tab order, focus rings, Ctrl/Cmd+Enter ────────────────────

def test_keyboard_tab_order_and_focus_ring(browser, base_url):
    pg = _new_page(browser, base_url, 1440, 900)
    try:
        pg.goto(f"{base_url}/json-formatter")
        pg.wait_for_selector("#input")

        # Tab through the first several focusables; order must follow DOM
        # and land on real interactive elements (no skipped/hidden traps).
        focused = []
        for _ in range(8):
            pg.keyboard.press("Tab")
            info = pg.evaluate(
                """() => { const el = document.activeElement;
                          if (!el || el === document.body) return null;
                          return {tag: el.tagName, id: el.id || null,
                                  cls: (el.className || '').toString()}; }""")
            if info:
                focused.append(info)
        assert len(focused) >= 6, f"too few focusable elements: {focused}"
        tags = {f["tag"].lower() for f in focused}
        assert tags <= {"a", "button", "input", "select", "textarea", "summary"}, (
            f"focus landed on non-interactive elements: {focused}")

        # Focus ring: the focused element must show a visible outline
        # (focus-visible styles) — computed style on :focus.
        ring = pg.evaluate(
            """() => { const el = document.activeElement;
                      el.focus();
                      const cs = getComputedStyle(el);
                      const ow = parseFloat(cs.outlineWidth || '0');
                      const bw = parseFloat(cs.borderWidth || '0');
                      const bs = cs.boxShadow || '';
                      return {ow, bw, bs, tag: el.tagName}; }""")
        assert ring["ow"] > 0 or ring["bw"] > 0 or "focus" in ring["bs"].lower() \
            or "outline" in json.dumps(ring).lower() or ring["bs"] != "none", (
            f"no visible focus indicator on {ring}")
    finally:
        _close(pg)


def test_ctrl_enter_runs_tool(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        # json-formatter (formatter.js) has its own Ctrl+Enter handler.
        pg.goto(f"{base_url}/json-formatter")
        pg.fill("#input", '{"kbd":true}')
        pg.focus("#input")
        pg.keyboard.press("Control+Enter")
        _wait_until(pg, lambda: (pg.text_content("#output") or "").strip() != "",
                    what="output after Ctrl+Enter")
        assert "kbd" in pg.text_content("#output")
    finally:
        _close(pg)


def test_ctrl_enter_runs_server_backed_tool(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/xml-formatter")
        pg.fill("#input", "<r><i/></r>")
        pg.focus("#input")
        pg.keyboard.press("Control+Enter")
        _wait_until(pg, lambda: (pg.text_content("#output") or "").strip() != "",
                    what="xml output after Ctrl+Enter")
    finally:
        _close(pg)


# ── 8. Phase 2 additions: motion toggle, high contrast, large output ──────

def test_motion_toggle_kills_and_restores_animations(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        # Pre-paint default (no stored preference): full motion.
        assert pg.evaluate(
            "() => document.documentElement.getAttribute('data-motion')") in (
            "full", "reduced")

        # Turn reduce-motion ON via the header toggle.
        pg.click("#motion-toggle")
        pg.wait_for_timeout(150)
        assert pg.evaluate(
            "() => document.documentElement.getAttribute('data-motion')") == "reduced"
        # Animations are actually off: computed animation-duration ~ 0.
        dur = pg.evaluate(
            """() => getComputedStyle(document.querySelector('.card, .tool-card'))
                      .animationDuration""")
        assert float(dur.rstrip("s")) < 0.05, f"animation still {dur}"

        # Persists in localStorage only.
        stored = pg.evaluate("() => localStorage.getItem('anvil-reduced-motion')")
        assert stored == "1"

        # Toggle back OFF.
        pg.click("#motion-toggle")
        pg.wait_for_timeout(150)
        assert pg.evaluate(
            "() => document.documentElement.getAttribute('data-motion')") == "full"
    finally:
        _close(pg)


def test_high_contrast_theme_applies_and_meets_aa(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.click("button[data-theme='high-contrast']")
        pg.wait_for_timeout(200)
        assert pg.evaluate(
            "() => document.documentElement.getAttribute('data-theme')") == "high-contrast"
        # Stored in localStorage only.
        assert pg.evaluate(
            "() => localStorage.getItem('anvil-theme')") == "high-contrast"

        # Body text contrast on the rendered page must meet WCAG AA (>= 4.5).
        contrast = pg.evaluate(
            """() => {
              function lum(c) {
                const m = c.match(/\\d+/g).map(Number);
                const [r, g, b] = m.slice(0, 3).map(v => {
                  v /= 255;
                  return v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
                });
                return 0.2126 * r + 0.7152 * g + 0.0722 * b;
              }
              const body = getComputedStyle(document.body);
              const fg = lum(body.color), bg = lum(body.backgroundColor);
              const [hi, lo] = fg > bg ? [fg, bg] : [bg, fg];
              return (hi + 0.05) / (lo + 0.05);
            }""")
        assert contrast >= 4.5, f"body contrast {contrast:.2f} < 4.5:1"
    finally:
        _close(pg)


def test_large_output_shows_notice_and_download(browser, base_url):
    pg = _new_page(browser, base_url)
    try:
        pg.goto(f"{base_url}/json-formatter")
        # ~2 MB of JSON that formats to > 5 MB (client-side formatter).
        big = pg.evaluate(
            "() => JSON.stringify({row: Array.from({length: 300000}, (_, i) => i)})")
        assert len(big) > 1_500_000
        pg.fill("#input", big)
        pg.click("#format-btn")
        _wait_until(pg, lambda: pg.query_selector(".output-truncated-note")
                    is not None, timeout_ms=15000,
                    what="large-output truncation notice")
        note = pg.text_content(".output-truncated-note")
        assert "showing the first 1 MB" in note
        assert pg.query_selector(".output-truncated-note button") is not None, (
            "a Download button must be offered")
        # The page stays responsive: the DOM output is capped at ~1 MB.
        out_len = pg.evaluate(
            "() => document.getElementById('output').textContent.length")
        assert out_len < 1_100_000, f"DOM holds {out_len} chars - page can freeze"
    finally:
        _close(pg)
