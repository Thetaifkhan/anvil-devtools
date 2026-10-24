"""Regression tests for defects confirmed by reproduction.

Each test encodes the failure mode that was reproduced before the fix. The
comments name the original defect so a future change that reintroduces it
fails loudly.
"""
import multiprocessing as mp
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from delivery import outbox  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY",
              "SUPABASE_SSL_ROOT_CERT", "IP_HMAC_SECRET", "TRUSTED_PROXY_COUNT"):
        monkeypatch.delenv(k, raising=False)
    outbox.configure(str(tmp_path / "outbox.sqlite3"), durable=True)
    import supabase_client
    supabase_client.reset_engine()
    yield


# ── 1. TLS must verify the server certificate ────────────────────────────────

class TestTlsVerification:
    def test_require_mode_is_refused(self):
        """Defect: sslmode=require encrypts without verifying the certificate."""
        from supabase_client import _secure_dsn
        assert _secure_dsn("postgresql://u:p@h:5432/d?sslmode=require", None) is None

    @pytest.mark.parametrize("mode", ["prefer", "allow", "disable"])
    def test_other_unverified_modes_are_refused(self, mode):
        from supabase_client import _secure_dsn
        assert _secure_dsn(f"postgresql://u:p@h:5432/d?sslmode={mode}", None) is None

    def test_default_is_verify_full(self, monkeypatch):
        from supabase_client import _secure_dsn
        dsn = _secure_dsn("postgresql://u:p@h:5432/d", None)
        assert "sslmode=verify-full" in dsn

    def test_verify_full_is_preserved(self):
        from supabase_client import _secure_dsn
        dsn = _secure_dsn("postgresql://u:p@h:5432/d?sslmode=verify-full", None)
        assert dsn.count("sslmode=") == 1 and "verify-full" in dsn

    def test_missing_root_cert_file_fails_closed(self, tmp_path):
        from supabase_client import _secure_dsn
        missing = str(tmp_path / "nope.crt")
        assert _secure_dsn("postgresql://u:p@h:5432/d", missing) is None

    def test_root_cert_is_attached(self, tmp_path):
        from urllib.parse import parse_qs, urlsplit
        from supabase_client import _secure_dsn
        ca = tmp_path / "ca.crt"
        ca.write_text("pem")
        dsn = _secure_dsn("postgresql://u:p@h:5432/d", str(ca))
        # urlencode percent-encodes the path, so compare the decoded value.
        assert parse_qs(urlsplit(dsn).query)["sslrootcert"] == [str(ca)]

    def test_database_url_from_env_is_hardened(self, monkeypatch):
        from supabase_client import _build_dsn
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/d?sslmode=require")
        assert _build_dsn() is None  # refused, not silently upgraded

    def test_parts_config_uses_configurable_host(self, monkeypatch):
        from supabase_client import _build_dsn
        monkeypatch.setenv("SUPABASE_DB_PASSWORD", "p@ss/word")
        monkeypatch.setenv("SUPABASE_DB_HOST", "pooler.example.test")
        dsn = _build_dsn()
        assert "@pooler.example.test:5432/" in dsn
        assert "p%40ss%2Fword" in dsn  # password URL-encoded
        assert "sslmode=verify-full" in dsn

    def test_verified_connection_against_untrusted_ca_is_refused(self):
        """Live TLS check: a server whose certificate is not from the configured
        CA must be refused under verify-full. Skips without a disposable server."""
        import psycopg
        ca_pem = Path("/tmp/tlsdemo/ca.crt")
        if not ca_pem.exists():
            pytest.skip("no disposable TLS fixture at /tmp/tlsdemo")
        port = int(os.environ.get("PG_PORT", "55432"))
        try:
            psycopg.connect(f"host=localhost port={port} user=postgres dbname=postgres "
                            f"connect_timeout=3 sslmode=verify-full", connect_timeout=3).close()
            pytest.skip("server does not require verified TLS in this environment")
        except psycopg.OperationalError as exc:
            assert "root certificate" in str(exc) or "certificate" in str(exc).lower()


# ── 2. Backoff must not overflow on long outages ─────────────────────────────

class TestBackoff:
    @pytest.mark.parametrize("attempt", [1023, 1024, 1100, 5000, 10**6])
    def test_large_attempt_counts_do_not_raise(self, attempt):
        """Defect: 2**n overflowed a float at n=1024 (OverflowError)."""
        from delivery.errors import BackoffConfig, compute_backoff
        d = compute_backoff(attempt, BackoffConfig(base_delay=2.0, max_delay=900.0))
        assert 0 < d <= 900.0


# ── 3. Authentication failures are transient config errors, not permanent ──

class TestAuthErrorClassification:
    @pytest.mark.parametrize("sqlstate", ["28P01", "28000"])
    def test_auth_failure_is_transient(self, sqlstate):
        """Defect: a password rotation dead-lettered every pending submission."""
        import psycopg.errors as pe
        from delivery.errors import classify_exception
        cls = pe.lookup(sqlstate)
        cls_err = cls("password authentication failed")
        assert classify_exception(cls_err).transient is True

    @pytest.mark.parametrize("sqlstate", ["42P01", "42703", "23505", "23502"])
    def test_schema_and_constraint_errors_are_permanent(self, sqlstate):
        """Defect: permanent SQLSTATEs were never recognised on psycopg 3 errors."""
        import psycopg.errors as pe
        from delivery.errors import classify_exception
        cls = classify_exception(pe.lookup(sqlstate)("db error"))
        assert cls.transient is False
        assert cls.code == sqlstate

    def test_transport_error_without_sqlstate_is_transient(self):
        import psycopg
        from delivery.errors import classify_exception
        assert classify_exception(psycopg.OperationalError("connection refused")).transient is True


# ── 4. Worker must not stop permanently ─────────────────────────────────────

class TestWorkerSurvivesFailures:
    def test_loop_keeps_running_after_many_iteration_failures(self, monkeypatch):
        """Defect: the loop returned after 50 consecutive failures, silently
        ending all retries while the queue still held items."""
        from delivery import worker

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("local sqlite briefly locked")

        monkeypatch.setattr(worker, "run_once", boom)
        monkeypatch.setattr(worker, "_poll_seconds", lambda: 0.001)
        # Shorten the production backoff cap so 60 retries fit the test window.
        monkeypatch.setattr(worker, "_ITERATION_BACKOFF_CAP_SECONDS", 0.01)
        worker._worker_stop.clear()
        t = threading.Thread(target=worker._worker_loop, daemon=True)
        t.start()
        deadline = time.time() + 30
        while calls["n"] < 60 and time.time() < deadline:
            time.sleep(0.01)
        alive_after_failures = t.is_alive()
        worker._worker_stop.set()
        t.join(timeout=5)
        assert calls["n"] >= 60, "loop did not keep attempting work"
        assert alive_after_failures, "worker thread exited after repeated failures"


# ── 5. Worker must be running in forked children (gunicorn workers) ────────

@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork unavailable")
def test_worker_starts_in_forked_child(monkeypatch, tmp_path):
    """Defect: a thread started before fork() looked alive in the child and
    start_worker() became a no-op, so delivery silently stopped."""
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "1")
    monkeypatch.setenv("SUPABASE_QUEUE_PATH", str(tmp_path / "fork.sqlite3"))
    from delivery import worker
    worker.start_worker()
    assert worker.is_worker_running()
    pid = os.fork()
    if pid == 0:  # child
        code = 0
        try:
            # The at-fork hook should have restarted the worker in this process.
            code = 0 if worker.is_worker_running() else 1
        finally:
            worker.stop_worker(timeout=1)
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    worker.stop_worker(timeout=2)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


# ── 6. Exclusive claims, leases, and schema upgrade ─────────────────────────

def _claim_worker(q, path):
    outbox.configure(path, durable=True)
    got = []
    for _ in range(200):
        items = outbox.claim_pending(batch_size=5)
        got.extend(i.event_key for i in items)
        for i in items:
            outbox.mark_delivered(i.id)
        if not items:
            time.sleep(0.001)
    q.put(got)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="fork-based test")
def test_concurrent_processes_never_double_claim(tmp_path):
    """Defect: separate autocommit SELECT and UPDATE let four processes claim
    the same item about 25% of the time (reproduced: 96-98 duplicates / 300)."""
    path = str(tmp_path / "race.sqlite3")
    outbox.configure(path, durable=True)
    for i in range(300):
        outbox.enqueue("contact_submission", {"n": i}, event_key=f"k{i}")
    ctx = mp.get_context("fork")
    q = ctx.Queue()
    procs = [ctx.Process(target=_claim_worker, args=(q, path)) for _ in range(4)]
    for p in procs:
        p.start()
    results = [q.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=30)
    claims = [k for r in results for k in r]
    assert len(claims) == 300
    assert len(set(claims)) == 300, "an item was claimed by more than one process"


def test_claim_is_leased_and_expired_lease_is_recovered(tmp_path):
    outbox.enqueue("contact_submission", {"x": 1}, event_key="lease-1")
    first = outbox.claim_pending(batch_size=5)
    assert [i.event_key for i in first] == ["lease-1"]
    # Leased: a second claim (even from the same process) must not see it.
    assert outbox.claim_pending(batch_size=5) == []
    # Simulate a crashed worker whose lease has expired.
    conn = sqlite3.connect(outbox.get_db_path())
    conn.execute("UPDATE outbox SET lease_expires_at = 0 WHERE event_key = 'lease-1'")
    conn.commit()
    conn.close()
    again = outbox.claim_pending(batch_size=5)
    assert [i.event_key for i in again] == ["lease-1"]
    assert again[0].attempt_count == 2


def test_terminal_transitions_release_the_lease(tmp_path):
    outbox.enqueue("contact_submission", {"x": 2}, event_key="lease-2")
    item = outbox.claim_pending(batch_size=1)[0]
    outbox.mark_delivered(item.id)
    conn = sqlite3.connect(outbox.get_db_path())
    row = conn.execute("SELECT lease_owner, lease_expires_at FROM outbox WHERE id=?",
                       (item.id,)).fetchone()
    conn.close()
    assert row == (None, None)


def test_pre_lease_database_is_upgraded_in_place(tmp_path):
    """Existing queues created before leases must keep their items after upgrade."""
    path = str(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
            next_attempt_at REAL NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT, last_error_at REAL, last_error_msg TEXT,
            delivered_at REAL, dead_lettered_at REAL, dead_letter_reason TEXT);
        INSERT INTO outbox (event_key, event_type, payload, created_at, next_attempt_at)
            VALUES ('legacy-1', 'contact_submission', '{"a":1}', 1, 1);
    """)
    conn.commit()
    conn.close()
    outbox.configure(path, durable=True)
    snap = outbox.counts()
    assert snap["pending"] == 1
    claimed = outbox.claim_pending(batch_size=5)
    assert [i.event_key for i in claimed] == ["legacy-1"]


# ── 7. Client identity: no spoofing, no shared buckets, no cookie bypass ───

class TestLimiterIdentity:
    def test_cookie_does_not_change_contact_limiter_key(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "x" * 32)
        from app import create_app
        from routes.contact import _limiter_key
        app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
        keys = set()
        for cookie in ("a", "b", "c"):
            with app.test_request_context("/api/contact", headers={"Cookie": f"anvil_anon={cookie}"},
                                          environ_base={"REMOTE_ADDR": "9.8.7.6"}):
                keys.add(_limiter_key())
        assert len(keys) == 1, "client-chosen cookie changed the rate-limit bucket"

    def test_distinct_clients_get_distinct_keys(self):
        from analytics.tracking import limiter_identity
        assert limiter_identity("", "1.1.1.1") != limiter_identity("", "2.2.2.2")

    def test_spoofed_forwarded_for_does_not_change_key(self):
        from analytics.tracking import limiter_identity
        # Peer 10.0.0.5; platform appends the real client 1.2.3.4.
        honest = limiter_identity("1.2.3.4", "10.0.0.5")
        spoofed = limiter_identity("77.77.77.77, 1.2.3.4", "10.0.0.5")
        assert honest == spoofed

    def test_no_shared_bucket_without_hmac_secret(self):
        """Defect: without IP_HMAC_SECRET every analytics client shared 'anon:unknown'."""
        from analytics.tracking import limiter_identity
        assert limiter_identity("", "1.1.1.1") != limiter_identity("", "")

    def test_limiter_keys_are_not_raw_ips(self):
        from analytics.tracking import limiter_identity
        assert "1.2.3.4" not in limiter_identity("", "1.2.3.4")


class TestAdminLoginThrottle:
    def test_one_client_cannot_lock_out_another(self, monkeypatch):
        """Defect: a single global key let one anonymous caller block all admin logins."""
        monkeypatch.setenv("SECRET_KEY", "x" * 32)
        from app import create_app
        app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
        c = app.test_client()
        from services.rate_limit import ADMIN_LIMITER
        ADMIN_LIMITER._buckets.clear()

        def seeded_post(ip):
            # Each request carries the session's CSRF token, as a real browser
            # would, so the request reaches the limiter.
            with c.session_transaction() as sess:
                sess["anvil_csrf"] = "t" * 43
            return c.post("/admin/login", json={}, headers={"X-CSRF-Token": "t" * 43},
                          environ_base={"REMOTE_ADDR": ip})

        statuses_a = [seeded_post("5.5.5.5").status_code for _ in range(25)]
        assert 429 in statuses_a, "attacker was never throttled"
        other = seeded_post("6.6.6.6")
        assert other.status_code != 429, "another client was locked out"


# ── 8. Rate-limited requests must not grow the queue ──────────────────────

def test_rate_limit_events_do_not_write_to_outbox():
    """Defect: every 429 enqueued an unbounded admin_audit row."""
    from routes import contact as contact_routes
    before = outbox.counts()["pending"]
    contact_routes._security_log_state["last"] = 0.0
    for _ in range(500):
        contact_routes._queue_security_event("rate_limited", "ip:abcdef0123456789")
    assert outbox.counts()["pending"] == before


# ── 9. Cookies and deployment configuration ───────────────────────────────

def test_session_cookie_is_secure_for_https_site(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "SITE_URL": "https://anvil.example.test"})
    assert app.config["SESSION_COOKIE_SECURE"] is True


def test_session_cookie_not_forced_secure_on_http(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32,
                      "SITE_URL": "http://localhost:5000"})
    assert app.config.get("SESSION_COOKIE_SECURE") in (False, None)


def test_dockerfile_does_not_preload_the_app():
    """Defect: --preload starts the worker thread in the master, which does not
    survive fork(), so workers ran without delivery."""
    text = (ROOT / "Dockerfile").read_text()
    cmd_lines = [l for l in text.splitlines() if l.startswith("CMD")]
    assert cmd_lines and "--preload" not in cmd_lines[0]


def test_render_config_runs_delivery_worker_in_process():
    """Defect: render.yaml shipped SUPABASE_WORKER_ENABLED=0 with the Background
    Worker commented out, so no process delivered queued submissions at all."""
    text = (ROOT / "render.yaml").read_text()
    web = text.split("  # ── Background Worker", 1)[0]
    assert '- key: SUPABASE_WORKER_ENABLED\n        value: "1"' in web
    assert '- key: WEB_CONCURRENCY\n        value: "1"' in web


def test_env_example_contains_no_real_secret_values():
    import re
    text = (ROOT / ".env.example").read_text()
    assert not re.search(r"eyJ[A-Za-z0-9_-]{20,}", text), "JWT-like value in .env.example"
    for m in re.finditer(r"postgresql://[^:\s]+:([^@\s]+)@", text):
        assert m.group(1) in {"URL_ENCODED_PASSWORD", "PASS", "YOUR_URL_ENCODED_PASSWORD"}, \
            "credential-looking value in DSN example"
