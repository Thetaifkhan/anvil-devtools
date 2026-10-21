"""Verify that all the bug fixes work as expected."""
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

failures: list[str] = []
passed: list[str] = []


def check(name, cond, detail=""):
    if cond:
        passed.append(name)
        print(f"  OK: {name}")
    else:
        failures.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL: {name} {detail}")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close(); return port


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env.update({
        "PORT": str(port),
        "FLASK_ENV": "development",
        "SECRET_KEY": "x" * 32,
        "SITE_URL": f"http://localhost:{port}",
    })
    proc = subprocess.Popen(
        [os.path.join(APP_DIR, ".venv", "bin", "gunicorn"), "-w", "2", "-b", f"127.0.0.1:{port}", "app:app"],
        cwd=APP_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1) as r:
                    if r.status == 200: break
            except Exception:
                time.sleep(0.25)

        # ---------- Server-side bug fixes ----------

        # BUG-28: SQL "capitalize" must now be accepted.
        from services import sql_service  # noqa
        result = sql_service.format_sql("select * from users", keyword_case="capitalize")
        check("BUG-28 sql capitalize works", "Select" in result and "From" in result, result)

        # Via API
        import urllib.request as ur
        req = ur.Request(f"{base}/api/sql-format", data=json.dumps({"text": "select 1", "action": "format", "keyword_case": "capitalize"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with ur.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        check("BUG-28 sql capitalize via API", data.get("ok") is True and "Select" in data.get("result", ""), data)

        # BUG-20: xml_to_json now captures mixed-content tail text.
        from services import conversion_service as c
        result = c.xml_to_json('<p>Hello <b>world</b>!</p>')
        parsed = json.loads(result)
        # The "!" must no longer be lost.
        check("BUG-20 xml_to_json keeps tail text", "#tails" in parsed and "!" in (parsed["#tails"] if isinstance(parsed["#tails"], str) else parsed["#tails"][0]), parsed)

        # BUG-21: xml_to_json preserves namespace prefix.
        result = c.xml_to_json('<root xmlns:a="http://a.com" xmlns:b="http://b.com"><a:item>x</a:item><b:item>y</b:item></root>')
        parsed = json.loads(result)
        check("BUG-21 xml_to_json preserves prefixes", "a:item" in parsed and "b:item" in parsed, parsed)

        # BUG-22: NaN/Infinity now rejected.
        try:
            c.json_to_xml('{"x": NaN}')
            check("BUG-22 NaN rejected", False, "should have raised")
        except Exception as e:
            check("BUG-22 NaN rejected", "NaN" in str(e) or "Infinity" in str(e), str(e))

        try:
            c.json_to_yaml('{"x": Infinity}')
            check("BUG-22 Infinity rejected", False, "should have raised")
        except Exception as e:
            check("BUG-22 Infinity rejected", "NaN" in str(e) or "Infinity" in str(e), str(e))

        try:
            c.json_to_csv('[{"x": -Infinity}]')
            check("BUG-22 -Infinity rejected", False, "should have raised")
        except Exception as e:
            check("BUG-22 -Infinity rejected", "NaN" in str(e) or "Infinity" in str(e), str(e))

        # ---------- Client-side bug fixes via Chromium ----------

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path="/home/z/.cache/ms-playwright/chromium-1200/chrome-linux64/chrome")
            ctx = browser.new_context(service_workers="block")
            page = ctx.new_page()
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(10000)

            # BUG-9: JWT decoder must not crash on null payload.
            errors = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto(f"{base}/jwt-decoder", wait_until="domcontentloaded")
            page.fill("textarea", "eyJhbGciOiAibm9uZSJ9.bnVsbA.abc")
            page.wait_for_timeout(300)
            # Click Decode button
            for b in page.query_selector_all("button"):
                try:
                    if "Decode" in (b.inner_text() or ""):
                        b.click(); break
                except Exception: pass
            page.wait_for_timeout(300)
            check("BUG-9 JWT null payload no crash", not errors, "; ".join(errors))
            # Should also render "(no claims)" message
            body = page.evaluate("() => document.body.innerText || ''")
            check("BUG-9 JWT null payload renders message", "null payload" in body.lower() or "no claims" in body.lower(), body[:300])

            # BUG-10: cron parser no longer produces nonsense for `0 */2 * * *`.
            page.goto(f"{base}/cron-parser", wait_until="domcontentloaded")
            page.fill("#input", "0 */2 * * *")
            page.wait_for_timeout(300)
            out = page.eval_on_selector("#output", "el => el.textContent")
            check("BUG-10 cron 0 */2 * * * no longer shows */2:00", "*/2:00" not in out, out)
            # Should describe "every 2 hours"
            check("BUG-10 cron 0 */2 * * * describes 2-hour step", "every 2 hour" in out.lower(), out)

            # BUG-10: `0 0 L * *` rejected with clear Quartz message
            page.fill("#input", "0 0 L * *")
            page.wait_for_timeout(300)
            out_status = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-10 cron L token rejected with Quartz message", "quartz" in out_status.lower() or "not supported" in out_status.lower(), out_status)

            # BUG-10: `99 99 * * *` rejected as out-of-range.
            page.fill("#input", "99 99 * * *")
            page.wait_for_timeout(300)
            out_status = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-10 cron 99 out-of-range rejected", "out of range" in out_status.lower() or "99" in out_status, out_status)

            # BUG-12: color converter no longer silently accepts rgb(256, 0, 0).
            page.goto(f"{base}/color-converter", wait_until="domcontentloaded")
            page.fill("#rgb", "rgb(256, 0, 0)")
            page.wait_for_timeout(300)
            hex_val = page.eval_on_selector("#hex", "el => el.value")
            check("BUG-12 color rgb(256,0,0) clamped (not #1000000)", hex_val.startswith("#") and len(hex_val) == 7 and hex_val != "#1000000", f"hex={hex_val}")
            # Should also display the out-of-range warning
            status_text = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-12 color out-of-range warning shown", "clamp" in status_text.lower() or "0–255" in status_text or "0-255" in status_text, status_text)

            # BUG-14: negative HEX rejected
            page.fill("#hex", "-FFFFF")
            page.wait_for_timeout(300)
            status_text = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-14 color -FFFFF rejected", "invalid" in status_text.lower() or "error" in status_text.lower(), status_text)

            # BUG-13: number base converter rejects invalid trailing digit.
            page.goto(f"{base}/number-base-converter", wait_until="domcontentloaded")
            page.fill("#bin", "12")
            page.wait_for_timeout(300)
            status_text = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-13 number_base rejects '12' in binary", "isn't a valid" in status_text.lower() or "invalid" in status_text.lower(), status_text)
            # Oct field should NOT have silently become "1"
            oct_val = page.eval_on_selector("#oct", "el => el.value")
            check("BUG-13 number_base oct doesn't truncate to '1'", oct_val != "1", f"oct={oct_val}")

            # BUG-36: tools without workbench support no longer show "Open in Workbench"
            for slug in ["color-converter", "number-base-converter", "timestamp-converter", "xml-diff"]:
                page.goto(f"{base}/{slug}", wait_until="domcontentloaded")
                page.wait_for_timeout(200)
                link = page.query_selector(".workbench-link")
                check(f"BUG-36 {slug} no workbench link", link is None, "link present")

            # Tools with workbench support still show the link
            for slug in ["json-formatter", "json-to-xml", "regex-tester", "uuid-generator"]:
                page.goto(f"{base}/{slug}", wait_until="domcontentloaded")
                page.wait_for_timeout(200)
                link = page.query_selector(".workbench-link")
                check(f"BUG-36 regression {slug} still has workbench link", link is not None, "link missing")

            # BUG-11: base64_decode offers download for non-UTF-8 bytes
            page.goto(f"{base}/base64-decode", wait_until="domcontentloaded")
            page.fill("textarea", "//79")  # invalid UTF-8 bytes 0xFF FF FF
            for b in page.query_selector_all("button"):
                try:
                    if "Decode" in (b.inner_text() or ""):
                        b.click(); break
                except: pass
            page.wait_for_timeout(500)
            body = page.evaluate("() => document.body.innerText || ''")
            # Should have either a download-binary link or a clear message
            check("BUG-11 base64_decode non-UTF8 shows download/binary message", "download binary" in body.lower() or "binary" in body.lower() or "not valid utf-8" in body.lower(), body[:300])

            # BUG-18: markdown preview doesn't italicize underscored identifiers.
            page.goto(f"{base}/markdown-preview", wait_until="domcontentloaded")
            page.fill("textarea", "my_var_name is here")
            page.wait_for_timeout(300)
            html = page.eval_on_selector("#preview", "el => el.innerHTML")
            check("BUG-18 markdown no <em> in my_var_name", "<em>var</em>" not in html and "my_var_name" in html.replace(" ", "_") or "<em>" not in html or "my<em>" not in html, html[:300])

            # BUG-1: json_diff with equal items in ignore-array-order mode
            page.goto(f"{base}/json-diff", wait_until="domcontentloaded")
            page.fill("#input-a", '[{"a":1},{"a":1}]')
            page.fill("#input-b", '[{"a":1},{"a":1}]')
            # Click ignore-array-order
            iao = page.query_selector("#ignore-array-order")
            if iao: iao.click()
            for b in page.query_selector_all("button"):
                try:
                    if "Compare" in (b.inner_text() or ""):
                        b.click(); break
                except: pass
            page.wait_for_timeout(500)
            status_text = page.eval_on_selector("#status", "el => el.textContent")
            check("BUG-1 json_diff equal arrays no diff", "0 difference" in status_text.lower() or "0 difference(s)" in status_text, status_text)

            # BUG-19: json_diff Copy button is disabled initially
            page.goto(f"{base}/json-diff", wait_until="domcontentloaded")
            disabled = page.eval_on_selector("#copy-btn", "el => el.disabled")
            check("BUG-19 json_diff Copy button disabled initially", disabled is True, f"disabled={disabled}")
            # After running a diff, it should be enabled
            page.fill("#input-a", '{"a":1}')
            page.fill("#input-b", '{"a":2}')
            for b in page.query_selector_all("button"):
                try:
                    if "Compare" in (b.inner_text() or ""):
                        b.click(); break
                except: pass
            page.wait_for_timeout(500)
            disabled2 = page.eval_on_selector("#copy-btn", "el => el.disabled")
            check("BUG-19 json_diff Copy button enabled after diff", disabled2 is False, f"disabled={disabled2}")

            # BUG-2: json_tree_viewer expand/collapse arrows update
            page.goto(f"{base}/json-tree-viewer", wait_until="domcontentloaded")
            page.fill("textarea", '{"a": {"b": 1, "c": 2}, "d": [3, 4]}')
            for b in page.query_selector_all("button"):
                try:
                    if "Render" in (b.inner_text() or ""):
                        b.click(); break
                except: pass
            page.wait_for_timeout(300)
            # Click Collapse all
            for b in page.query_selector_all("button"):
                try:
                    if "Collapse" in (b.inner_text() or ""):
                        b.click(); break
                except: pass
            page.wait_for_timeout(200)
            toggles = page.eval_on_selector_all(".toggle", "els => els.map(e => e.textContent)")
            # After collapse, the root toggle should be "▸" (collapsed)
            check("BUG-2 collapse-all updates root toggle to ▸", len(toggles) > 0 and toggles[0] == "▸", f"toggles={toggles}")

            browser.close()

        print()
        print(f"=== PASSED: {len(passed)} ===")
        if failures:
            print(f"=== FAILED: {len(failures)} ===")
            for f in failures:
                print("  - " + f)
            return 1
        print("=== ALL BUG FIX VERIFICATIONS PASSED ===")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
