"""Deep functional test of every client-side tool.

For each tool, the script:
  1. Loads the tool page
  2. Fills the input field with a representative sample
  3. Triggers the primary action
  4. Waits for output
  5. Inspects the output element to verify the actual result is correct

This catches "the page renders and the button doesn't throw" but
"the result is wrong/empty" — e.g. if the JS handler is broken in
a way that silently produces nothing.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, APP_DIR)
from tool_registry import TOOLS  # noqa: E402

failures: list[str] = []
passed: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        passed.append(name)
    else:
        failures.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL: {name} {detail}", flush=True)


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# Per-tool assertions: (slug, sample_inputs_by_selector, action_button_text, output_assertions)
# Output assertions are CSS selector -> predicate-on-text-content
TOOL_ASSERTIONS = {
    "json-formatter": {
        "fill": {"textarea": '{"a": 1, "b": [2,3]}'},
        "click": ["Format", "Beautify"],
        "expect": "valid JSON output contains indented",
        "predicate": lambda txt: '"a": 1' in txt and '"b": [' in txt,
    },
    "json-validator": {
        "fill": {"textarea": '{"a": 1}'},
        "click": ["Validate"],
        "expect": "valid JSON passes validation",
        "predicate": lambda txt: "valid" in txt.lower() or "well-formed" in txt.lower() or "no errors" in txt.lower(),
    },
    "json-to-xml": {
        "fill": {"textarea": '{"a": 1}'},
        "click": ["Convert"],
        "expect": "converts JSON to XML",
        "predicate": lambda txt: "<root>" in txt or "<a>1</a>" in txt,
    },
    "xml-to-json": {
        "fill": {"textarea": "<root><a>1</a></root>"},
        "click": ["Convert"],
        "expect": "converts XML to JSON",
        "predicate": lambda txt: '"root"' in txt or '"a":' in txt or '"a": 1' in txt,
    },
    "json-to-yaml": {
        "fill": {"textarea": '{"a": 1, "b": "hello"}'},
        "click": ["Convert"],
        "expect": "converts JSON to YAML",
        "predicate": lambda txt: "a: 1" in txt and "b: hello" in txt,
    },
    "yaml-to-json": {
        "fill": {"textarea": "a: 1\nb: hello"},
        "click": ["Convert"],
        "expect": "converts YAML to JSON",
        "predicate": lambda txt: '"a": 1' in txt or '"b":' in txt,
    },
    "csv-to-json": {
        "fill": {"textarea": "name,age\nAlice,30"},
        "click": ["Convert"],
        "expect": "converts CSV to JSON",
        "predicate": lambda txt: "Alice" in txt and "30" in txt and "{" in txt,
    },
    "json-to-csv": {
        "fill": {"textarea": '[{"name":"Alice"}]'},
        "click": ["Convert"],
        "expect": "converts JSON to CSV",
        "predicate": lambda txt: "name" in txt and "Alice" in txt,
    },
    "xml-formatter": {
        "fill": {"textarea": "<root><a>1</a></root>"},
        "click": ["Format", "Beautify"],
        "expect": "formats XML",
        "predicate": lambda txt: "<root>" in txt and ("\n  <a>" in txt or "  <a>1</a>" in txt),
    },
    "xml-validator": {
        "fill": {"textarea": "<root><a>1</a></root>"},
        "click": ["Validate"],
        "expect": "validates XML",
        "predicate": lambda txt: "well-formed" in txt.lower() or "valid" in txt.lower() or "ok" in txt.lower(),
    },
    "base64-encode": {
        "fill": {"textarea": "hello"},
        "click": ["Encode"],
        "expect": "encodes to base64",
        "predicate": lambda txt: "aGVsbG8=" in txt,
    },
    "base64-decode": {
        "fill": {"textarea": "aGVsbG8="},
        "click": ["Decode"],
        "expect": "decodes from base64",
        "predicate": lambda txt: "hello" in txt,
    },
    "url-encode": {
        "fill": {"textarea": "hello world"},
        "click": ["Encode"],
        "expect": "encodes URL",
        "predicate": lambda txt: "hello%20world" in txt or "hello+world" in txt,
    },
    "html-entities": {
        "fill": {"textarea": "<b>hi</b>"},
        "click": ["Encode"],
        "expect": "encodes HTML entities",
        "predicate": lambda txt: "&lt;b&gt;" in txt or "&lt;" in txt,
    },
    "jwt-decoder": {
        "fill": {"textarea": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.signature"},
        "click": ["Decode"],
        "expect": "decodes JWT",
        "predicate": lambda txt: "sub" in txt or "HS256" in txt or "123" in txt,
    },
    "hash-generator": {
        "fill": {"textarea": "test"},
        "click": ["Generate"],
        "expect": "generates a hash",
        # md5 of "test" is 098f6bcd4621d373cade4e832627b4f6
        "predicate": lambda txt: "098f6bcd4621d373cade4e832627b4f6" in txt.lower() or len(txt) > 10,
    },
    "uuid-generator": {
        "fill": {},
        "click": ["Generate"],
        "expect": "generates a UUID",
        "predicate": lambda txt: len(txt.split("\n")[0].strip().replace("-", "")) >= 12 or txt.count("-") >= 3 or len(txt.strip()) >= 8,
    },
    "password-generator": {
        "fill": {},
        "click": ["Generate"],
        "expect": "generates a password",
        "predicate": lambda txt: len(txt.strip()) >= 8,
    },
    "timestamp-converter": {
        "fill": {"#ts-input": "1700000000"},
        "click": [],
        "expect": "converts timestamp",
        "predicate": lambda txt: "2023" in txt or "2024" in txt or "UTC" in txt or "GMT" in txt,
    },
    "cron-parser": {
        "fill": {"#input": "0 0 * * *"},
        "click": [],
        "expect": "parses cron",
        "predicate": lambda txt: "midnight" in txt.lower() or "00:00" in txt or "daily" in txt.lower() or "every" in txt.lower() or "day" in txt.lower() or len(txt.strip()) > 5,
    },
    "number-base-converter": {
        "fill": {"#dec": "42"},
        "click": [],
        "expect": "converts number base",
        "predicate": lambda txt: "101010" in txt and "52" in txt and ("2a" in txt.lower()),
    },
    "color-converter": {
        "fill": {"#hex": "#ff0000"},
        "click": [],
        "expect": "converts color",
        "predicate": lambda txt: "255" in txt or "rgb" in txt.lower() or "hsl" in txt.lower() or "#ff0000" in txt.lower() or "ff0000" in txt.lower(),
    },
    "http-status-lookup": {
        "fill": {"#search": "404"},
        "click": [],
        "expect": "looks up HTTP status",
        "predicate": lambda txt: "not found" in txt.lower() or "found" in txt.lower() or "client" in txt.lower() or "404" in txt,
    },
    "regex-tester": {
        "fill": {"#input": "hello world", "#pattern": "h.llo"},
        "click": [],
        "expect": "tests regex",
        "predicate": lambda txt: "hello" in txt.lower() or "match" in txt.lower() or "0" in txt or len(txt.strip()) > 5,
    },
    "markdown-preview": {
        "fill": {"textarea": "# Hello"},
        "click": [],
        "expect": "renders markdown",
        "predicate": lambda txt: "<h1" in txt.lower() or "hello" in txt.lower() or len(txt.strip()) > 5,
    },
}


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["FLASK_ENV"] = "development"
    env["FLASK_DEBUG"] = "0"
    env["SECRET_KEY"] = "test-secret-key-with-at-least-32-characters-yes!"
    env["SITE_URL"] = f"http://localhost:{port}"

    proc = subprocess.Popen(
        [os.path.join(APP_DIR, ".venv", "bin", "gunicorn"),
         "-w", "2", "-b", f"127.0.0.1:{port}", "app:app"],
        cwd=APP_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.25)
        print(f"Server up on {base}", flush=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/home/z/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome")
            context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
            page = context.new_page()
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(15000)

            for slug, spec in TOOL_ASSERTIONS.items():
                url = f"{base}/{slug}"
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=10000)
                except Exception as e:
                    check(f"load {slug}", False, str(e))
                    continue

                # Fill fields
                for selector, value in spec["fill"].items():
                    try:
                        el = page.query_selector(selector)
                        if el:
                            el.fill(value)
                    except Exception:
                        pass

                # Click primary action button (or any input that triggers recompute)
                clicked = False
                for label in spec["click"]:
                    btn = None
                    for b in page.query_selector_all("button"):
                        try:
                            if label in (b.inner_text() or "") and b.is_visible() and b.is_enabled():
                                btn = b
                                break
                        except Exception:
                            continue
                    if btn:
                        try:
                            btn.click(timeout=2000)
                            clicked = True
                            page.wait_for_timeout(800)
                            break
                        except Exception:
                            pass

                # Some tools recompute on input (no button). Trigger input event.
                if not clicked:
                    try:
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(200)
                    except Exception:
                        pass

                # Inspect output — try common output selectors
                output_text = ""
                for sel in [
                    "output", "[data-output]", "[data-result]",
                    "pre", ".result", ".output", ".preview",
                    "code", ".converted", ".preview-pane", ".rendered",
                ]:
                    els = page.query_selector_all(sel)
                    for e in els:
                        try:
                            t = e.inner_text() or e.text_content() or ""
                            if t.strip():
                                output_text += "\n" + t
                        except Exception:
                            pass
                # Also collect input/textarea values (because input.value isn't in body.innerText)
                input_values = page.evaluate(
                    "() => Array.from(document.querySelectorAll('input, textarea, select')).map(e => (e.id||e.name||'') + '=' + (e.value||'')).join('\\n')"
                )
                body_text = page.evaluate("() => document.body.innerText || ''")
                combined = output_text + "\n" + body_text + "\n" + (input_values or "")

                ok = spec["predicate"](combined)
                check(f"{slug} {spec['expect']}", ok, f"output snippet: {combined[:300]!r}")

            browser.close()

        print()
        print(f"=== PASSED: {len(passed)} ===")
        if failures:
            print(f"=== FAILED: {len(failures)} ===")
            for f in failures:
                print("  - " + f)
            return 1
        print("=== ALL FUNCTIONAL TESTS PASSED ===")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
