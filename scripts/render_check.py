"""Render every tool page in headless Chromium against a live Anvil server.

Captures:
  - HTTP status (must be 200)
  - console errors
  - uncaught JS exceptions
  - horizontal overflow at common viewport widths (320, 375, 768, 1024, 1440)

Also clicks the tool's primary action button (if any) to flush out runtime bugs.
"""
from __future__ import annotations

import json
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
    port = s.getsockname()[1]
    s.close()
    return port


SAMPLE_INPUTS = {
    "json-formatter": '{"a": 1, "b": [2, 3]}',
    "json-validator": '{"a": 1}',
    "json-tree-viewer": '{"a": 1, "b": [2, 3]}',
    "json-diff": '{"a": 1}',
    "json-to-xml": '{"a": 1}',
    "xml-to-json": "<root><a>1</a></root>",
    "json-to-yaml": '{"a": 1}',
    "yaml-to-json": "a: 1",
    "csv-to-json": "a,b\n1,2",
    "json-to-csv": '[{"a":1}]',
    "xml-formatter": "<root><a>1</a></root>",
    "xml-validator": "<root><a>1</a></root>",
    "xml-diff": "<root><a>1</a></root>",
    "sql-formatter": "select * from users",
    "base64-encode": "hello world",
    "base64-decode": "aGVsbG8gd29ybGQ=",
    "url-encode": "https://example.com/?q=hello world",
    "html-entities": "<b>hello & goodbye</b>",
    "jwt-decoder": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.signature",
    "hash-generator": "test",
    "uuid-generator": "",
    "password-generator": "",
    "timestamp-converter": "1700000000",
    "cron-parser": "0 0 * * *",
    "regex-tester": "hello world",
    "text-diff": "hello\nworld",
    "markdown-preview": "# Hello\n\nworld",
    "http-status-lookup": "404",
    "number-base-converter": "42",
    "color-converter": "#ff0000",
    "formatter": '{"a": 1}',
    "text-diff-second": "world\nhello",
}


def fill_inputs(page, slug):
    """Fill any textarea / text input with a sample value if empty."""
    inputs = page.query_selector_all("textarea")
    for i, ta in enumerate(inputs[:2]):
        try:
            if not ta.input_value():
                if slug == "text-diff" and i == 1:
                    ta.fill(SAMPLE_INPUTS["text-diff-second"])
                else:
                    ta.fill(SAMPLE_INPUTS.get(slug, ""))
        except Exception:
            pass
    inputs = page.query_selector_all("input[type='text'], input:not([type])")
    for inp in inputs[:3]:
        try:
            if not inp.input_value():
                inp.fill(SAMPLE_INPUTS.get(slug, ""))
        except Exception:
            pass


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
            browser = p.chromium.launch(
                executable_path="/home/z/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome",
                args=["--disable-features=site-per-process"],
            )
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                ignore_https_errors=True,
                service_workers="block",  # disable service worker so the page closes promptly
            )
            page = context.new_page()
            page.set_default_timeout(8000)
            page.set_default_navigation_timeout(15000)

            console_msgs: list[str] = []
            page_errors: list[str] = []

            def on_console(msg):
                if msg.type in ("error", "warning"):
                    console_msgs.append(f"[{msg.type}] {msg.text}")

            def on_pageerror(err):
                page_errors.append(str(err))

            page.on("console", on_console)
            page.on("pageerror", on_pageerror)

            keywords = ["Format", "Generate", "Convert", "Encode", "Decode", "Parse", "Test", "Compare", "Validate", "Beautify", "Minify", "Preview", "Lookup", "Run", "Submit"]

            for tool in TOOLS:
                slug = tool["slug"]
                console_msgs.clear()
                page_errors.clear()
                url = f"{base}/{slug}"
                try:
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=12000)
                except Exception as e:
                    check(f"load {slug}", False, f"navigation error: {e}")
                    continue
                check(f"load {slug}", resp and resp.status == 200, f"status={resp.status if resp else None}")
                check(f"{slug} no JS exceptions", not page_errors, "; ".join(page_errors)[:300])

                real_console_errors = [m for m in console_msgs if "ERR_BLOCKED_BY_CLIENT" not in m and "net::ERR" not in m and "Failed to load resource" not in m and "Service Worker registration blocked by Playwright" not in m and "service worker" not in m.lower()]
                check(f"{slug} no console errors", not real_console_errors, "; ".join(real_console_errors)[:300])

                # Try to trigger the tool's primary action
                try:
                    fill_inputs(page, slug)
                    buttons = page.query_selector_all("button")
                    primary = None
                    for b in buttons:
                        try:
                            txt = (b.inner_text() or "").strip()
                        except Exception:
                            continue
                        if any(kw in txt for kw in keywords):
                            if b.is_visible() and b.is_enabled():
                                primary = b
                                break
                    if primary:
                        try:
                            primary.click(timeout=2000)
                        except Exception:
                            pass
                        page.wait_for_timeout(800)
                        check(f"{slug} action no JS error", not page_errors, "; ".join(page_errors)[:300])
                        real_console_errors = [m for m in console_msgs if "ERR_BLOCKED_BY_CLIENT" not in m and "net::ERR" not in m and "Failed to load resource" not in m and "Service Worker registration blocked by Playwright" not in m and "service worker" not in m.lower()]
                        check(f"{slug} action no console error", not real_console_errors, "; ".join(real_console_errors)[:300])
                except Exception as e:
                    check(f"{slug} action", False, f"click error: {e}")

                # Horizontal overflow check at multiple widths
                for w in (320, 375, 768, 1024, 1440):
                    page.set_viewport_size({"width": w, "height": 900})
                    page.wait_for_timeout(80)
                    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                    check(f"{slug} no h-overflow @ {w}px", overflow <= 1, f"overflow={overflow}px")

                print(f"  ok: {slug} (page + action + 5 widths)", flush=True)

            browser.close()

        print()
        print(f"=== PASSED: {len(passed)} ===")
        if failures:
            print(f"=== FAILED: {len(failures)} ===")
            for f in failures:
                print("  - " + f)
            return 1
        print("=== ALL RENDER CHECKS PASSED ===")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
