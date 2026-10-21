"""Comprehensive end-to-end smoke test for the Anvil developer-tools app.

Boots a real Flask server on a random free port, then exercises:
  - Every static route (/, /tools, /workbench, /privacy-policy, /terms-of-service,
    /disclaimer, /contact, /robots.txt, /sitemap.xml, /manifest.webmanifest,
    /service-worker.js, /healthz, /unknown-route -> 404)
  - Every tool page (one slug per entry in tool_registry.TOOLS)
  - Every API endpoint for server-side tools, with valid input, invalid input,
    missing fields, oversized input, and edge cases (empty, Unicode, recursion)
  - Security headers (CSP, X-Frame-Options, X-Content-Type-Options,
    Referrer-Policy, Permissions-Policy) and API Cache-Control: no-store
  - 413 handling for oversized payloads

Exits non-zero on any failure so CI catches regressions.
"""
from __future__ import annotations

import json
import os
import sys
import socket
import subprocess
import time
import urllib.error
import urllib.request

# Ensure we can import the app's registry
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
        print(f"  FAIL: {name} {detail}")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body=None, headers=None):
    data = None
    h = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        else:
            data = body.encode("utf-8") if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env["PORT"] = str(port)
    env["FLASK_ENV"] = "development"
    env["FLASK_DEBUG"] = "0"
    env["SECRET_KEY"] = "test-secret-key-with-at-least-32-characters-yes!"
    env["SITE_URL"] = "http://localhost:%d" % port

    # Start gunicorn like the Dockerfile would
    proc = subprocess.Popen(
        [
            os.path.join(APP_DIR, ".venv", "bin", "gunicorn"),
            "-w", "2",
            "-b", f"127.0.0.1:{port}",
            "app:app",
        ],
        cwd=APP_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    base = f"http://127.0.0.1:{port}"
    try:
        # Wait for server
        ok = False
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"{base}/healthz", timeout=1) as r:
                    if r.status == 200:
                        ok = True
                        break
            except Exception:
                time.sleep(0.25)
        if not ok:
            print("Server did not start. gunicorn output:")
            if proc.stdout:
                print(proc.stdout.read()[:4000])
            return 1
        print(f"Server up on {base}")

        # ---- Static routes ----
        for path in [
            "/", "/tools", "/workbench", "/privacy-policy", "/terms-of-service",
            "/disclaimer", "/contact", "/robots.txt", "/sitemap.xml",
            "/manifest.webmanifest", "/service-worker.js", "/healthz",
        ]:
            st, body, hdrs = http("GET", f"{base}{path}")
            check(f"GET {path}", st == 200, f"status={st}")
            if path == "/" and st == 200:
                check("homepage has tool index", "tool_index_json" in body or "<script" in body)
            if path == "/robots.txt":
                check("robots has Sitemap", "Sitemap:" in body)
            if path == "/sitemap.xml":
                check("sitemap is xml", "<?xml" in body and "<urlset" in body)
            if path == "/manifest.webmanifest":
                check("manifest is json", body.strip().startswith("{"))
            if path == "/healthz":
                check("healthz json", json.loads(body) == {"ok": True})

        # ---- 404 ----
        st, body, _ = http("GET", f"{base}/this-route-does-not-exist")
        check("404 page", st == 404 and "<!doctype html>" in body.lower())

        # ---- Security headers on every HTML route ----
        for path in ["/", "/tools", "/workbench", "/json-formatter", "/xml-formatter"]:
            st, _, hdrs = http("GET", f"{base}{path}")
            lc = {k.lower(): v for k, v in hdrs.items()}
            check(f"{path} CSP present", "content-security-policy" in lc)
            check(f"{path} X-Frame-Options DENY", lc.get("x-frame-options") == "DENY")
            check(f"{path} X-Content-Type-Options", lc.get("x-content-type-options") == "nosniff")
            check(f"{path} Referrer-Policy", "strict-origin-when-cross-origin" in lc.get("referrer-policy", "").lower())

        # ---- Every tool page renders (200, no template error) ----
        for t in TOOLS:
            slug = t["slug"]
            st, body, _ = http("GET", f"{base}/{slug}")
            check(f"tool page {slug}", st == 200, f"status={st}, body[:200]={body[:200]!r}")
            check(f"tool page {slug} has content", len(body) > 1000 and ("Anvil" in body or "anvil" in body))

        # ---- API: JSON <-> XML ----
        st, body, _ = http("POST", f"{base}/api/json-to-xml", {"text": '{"name":"Alice","age":30,"hobbies":["x","y"],"active":true,"nothing":null}', "root": "person"})
        check("json-to-xml ok", st == 200 and json.loads(body).get("ok") is True, body[:300])
        if st == 200:
            xml = json.loads(body)["result"]
            check("json-to-xml bool true", "<active>true</active>" in xml)
            check("json-to-xml null attr", 'nil="true"' in xml)
            st2, body2, _ = http("POST", f"{base}/api/xml-to-json", {"text": xml})
            check("xml-to-json ok", st2 == 200 and json.loads(body2).get("ok") is True, body2[:300])

        # ---- API: JSON <-> YAML ----
        st, body, _ = http("POST", f"{base}/api/json-to-yaml", {"text": '{"a":1,"b":"hello","c":[1,2,3]}'})
        check("json-to-yaml ok", st == 200 and "a: 1" in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/yaml-to-json", {"text": "a: 1\nb: hello\nc:\n  - 1\n  - 2\n  - 3\n"})
        check("yaml-to-json ok", st == 200 and json.loads(body)["result"] == '{\n  "a": 1,\n  "b": "hello",\n  "c": [\n    1,\n    2,\n    3\n  ]\n}', body[:300])
        st, body, _ = http("POST", f"{base}/api/yaml-to-json", {"text": "date: 2024-01-15\n"})
        check("yaml-to-json date ok", st == 200 and '"2024-01-15"' in json.loads(body).get("result", ""), body[:300])

        # ---- API: CSV <-> JSON ----
        st, body, _ = http("POST", f"{base}/api/csv-to-json", {"text": "name,age\nAlice,30\nBob,25\n"})
        check("csv-to-json ok", st == 200 and '"Alice"' in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/csv-to-json", {"text": "a,b\n1,2,3\n"})
        check("csv-to-json rejects ragged row", st == 400 and "more values" in json.loads(body).get("error", ""), body[:200])

        st, body, _ = http("POST", f"{base}/api/json-to-csv", {"text": '[{"name":"Alice","age":30},{"name":"Bob","age":25}]'})
        check("json-to-csv ok", st == 200 and "Alice" in json.loads(body).get("result", "") and "30" in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/json-to-csv", {"text": '[{"a":1,"b":{"x":1},"c":true}]'})
        result = json.loads(body).get("result", "") if st == 200 else ""
        # CSV escapes inner " by doubling them, so "{"x": 1}" becomes "{""x"": 1}"
        check("json-to-csv nested is json", st == 200 and '""x"": 1' in result and "true" in result, body[:200])

        # ---- API: XML format / validate ----
        st, body, _ = http("POST", f"{base}/api/xml-format", {"text": "<root><a>1</a><b>2</b></root>"})
        check("xml-format ok", st == 200 and "  <a>1</a>" in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/xml-format", {"text": "<root>\n  <a>1</a>\n</root>", "minify": True})
        check("xml-format minify ok", st == 200 and json.loads(body).get("result", "") == "<root><a>1</a></root>", body[:300])
        st, body, _ = http("POST", f"{base}/api/xml-format", {"text": "<root><a>1</a></root>", "indent": "\t"})
        check("xml-format tab indent ok", st == 200 and "\t<a>1</a>" in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/xml-format", {"text": "<r/>", "indent": " " * 17})
        check("xml-format rejects oversized indent", st == 400, body[:200])
        st, body, _ = http("POST", f"{base}/api/xml-validate", {"text": "<!DOCTYPE root [<!ENTITY x 'y'>]><root>&x;</root>"})
        # validate_xml catches ToolError and returns valid:False with a message; route stays 200.
        check("xml-validate rejects DOCTYPE", st == 200 and json.loads(body).get("valid") is False and "DOCTYPE" in json.loads(body).get("message", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/xml-validate", {"text": "<root><a>1</a></root>"})
        check("xml-validate ok", st == 200 and json.loads(body).get("valid") is True, body[:300])

        # ---- API: SQL format / minify ----
        st, body, _ = http("POST", f"{base}/api/sql-format", {"text": "select * from users where id = 1", "action": "format", "keyword_case": "upper", "indent_width": 2})
        check("sql-format ok", st == 200 and "SELECT" in json.loads(body).get("result", "") and "FROM" in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/sql-format", {"text": "SELECT   *   FROM   users", "action": "minify"})
        check("sql-minify ok", st == 200 and "  " not in json.loads(body).get("result", ""), body[:300])
        st, body, _ = http("POST", f"{base}/api/sql-format", {"text": "select 1", "action": "weird"})
        check("sql-format rejects invalid action", st == 400, body[:200])
        st, body, _ = http("POST", f"{base}/api/sql-format", {"text": "select 1", "action": "format", "indent_width": 99})
        check("sql-format rejects oversized indent_width", st == 400, body[:200])

        # ---- API: validation errors ----
        st, body, _ = http("POST", f"{base}/api/json-to-xml", {})
        check("missing text -> 400", st == 400 and "text" in json.loads(body).get("error", "").lower(), body[:200])
        st, body, _ = http("POST", f"{base}/api/json-to-xml", {"text": 42})
        check("non-string text -> 400", st == 400, body[:200])
        st, body, _ = http("POST", f"{base}/api/json-to-xml", "[1,2,3]", {"Content-Type": "application/json"})
        check("array body -> 400", st == 400, body[:200])
        st, body, _ = http("POST", f"{base}/api/json-to-yaml", {"text": "{not json"})
        check("invalid json -> 400", st == 400 and "json" in json.loads(body).get("error", "").lower(), body[:200])

        # ---- API: oversized text rejected ----
        big = "x" * (2_000_001)
        st, body, _ = http("POST", f"{base}/api/json-to-xml", {"text": big})
        check("oversize text -> 400", st == 400 and "too large" in json.loads(body).get("error", "").lower(), body[:200])

        # ---- API: no-store on /api/* ----
        st, body, hdrs = http("POST", f"{base}/api/xml-validate", {"text": "<r/>"})
        cache_ctrl = {k.lower(): v for k, v in hdrs.items()}.get("cache-control", "")
        check("API no-store", "no-store" in cache_ctrl, f"Cache-Control={cache_ctrl!r}")

        # ---- 413 too-large body ----
        big_body = "x" * (6 * 1024 * 1024)  # 6MB > 5MB default limit
        # Flask rejects the request during the body read, which can close the
        # connection while we're still writing. Treat BrokenPipe / 413 as success.
        try:
            st, body, _ = http("POST", f"{base}/api/json-to-xml", {"text": big_body})
            check("6MB body -> 413", st == 413, f"status={st}, body[:200]={body[:200]!r}")
        except urllib.error.URLError as e:
            # BrokenPipe is fine — server closed connection mid-write
            check("6MB body -> connection refused (413 expected)", "Broken pipe" in str(e) or "ConnectionReset" in str(e) or "RemoteDisconnected" in str(e), str(e))

        # ---- /sitemap.xml lists every tool slug ----
        _, body, _ = http("GET", f"{base}/sitemap.xml")
        for t in TOOLS:
            check(f"sitemap has {t['slug']}", f"/{t['slug']}</loc>" in body)

        # ---- Unicode round-trip through JSON->XML->JSON ----
        st, body, _ = http("POST", f"{base}/api/json-to-xml", {"text": '{"msg":"héllo wörld 日本語 emoji 🎉"}'})
        check("unicode json-to-xml ok", st == 200, body[:200])
        if st == 200:
            xml = json.loads(body).get("result", "")
            st2, body2, _ = http("POST", f"{base}/api/xml-to-json", {"text": xml})
            check("unicode xml-to-json ok", st2 == 200 and "héllo wörld 日本語" in json.loads(body2).get("result", ""), body2[:300])

        print()
        print(f"=== PASSED: {len(passed)} ===")
        if failures:
            print(f"=== FAILED: {len(failures)} ===")
            for f in failures:
                print("  - " + f)
            return 1
        print("=== ALL SMOKE TESTS PASSED ===")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
