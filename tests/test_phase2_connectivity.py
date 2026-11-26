"""Phase 2 §2.2 connectivity hardening — tests.

Covers: pool recycling parameters (max_idle / max_lifetime), the health
state machine (healthy -> degraded -> down -> cooldown -> probe), fast-fail
while down, the IPv6-only pooler warning, and existing behaviour that must
not regress (timeouts, TLS refusal, boot-without-database).
"""
from __future__ import annotations

import os
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import supabase_client


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_SSL_ROOT_CERT",
              "SUPABASE_POOL_MAX_IDLE", "SUPABASE_POOL_MAX_LIFETIME"):
        monkeypatch.delenv(k, raising=False)
    supabase_client.reset_engine()
    supabase_client.reset_health()
    yield
    supabase_client.reset_engine()
    supabase_client.reset_health()


# ── 1. Pool recycling parameters ───────────────────────────────────────────

def test_pool_has_recycling_parameters(monkeypatch):
    """max_idle (60 s) and max_lifetime (30 min) must be passed to the pool."""
    captured = {}

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connection(self):
            raise AssertionError("not used")

        def close(self):
            pass

    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:PLACEHOLDER@127.0.0.1:5432/db")
    with patch.object(supabase_client, "ConnectionPool", FakePool):
        try:
            with supabase_client.cursor():
                pass
        except Exception:
            pass  # FakePool.connection raises; we only need __init__ kwargs

    assert captured.get("max_idle") == 60
    assert captured.get("max_lifetime") == 1800
    assert captured.get("min_size") == 1
    assert captured.get("max_size") == 5
    assert captured.get("timeout") == 10


def test_pool_recycling_is_configurable_and_bounded(monkeypatch):
    captured = {}

    class FakePool:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def connection(self):
            raise RuntimeError("stop")

        def close(self):
            pass

    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:PLACEHOLDER@127.0.0.1:5432/db")
    monkeypatch.setenv("SUPABASE_POOL_MAX_IDLE", "999999")
    monkeypatch.setenv("SUPABASE_POOL_MAX_LIFETIME", "-5")
    with patch.object(supabase_client, "ConnectionPool", FakePool):
        try:
            with supabase_client.cursor():
                pass
        except Exception:
            pass
    # _int_env clamps: idle [1, 3600], lifetime [60, 86400].
    assert captured["max_idle"] == 3600
    assert captured["max_lifetime"] == 60


# ── 2. Health state machine ────────────────────────────────────────────────

def test_one_failure_degrades():
    supabase_client._record_failure(ConnectionError("blip"))
    snap = supabase_client.health_state()
    assert snap["state"] == "degraded"
    assert snap["consecutive_failures"] == 1


def test_three_failures_within_window_go_down_and_skip_calls():
    for _ in range(3):
        supabase_client._record_failure(ConnectionError("boom"))
    snap = supabase_client.health_state()
    assert snap["state"] == "down"
    assert snap["cooldown_remaining_seconds"] > 0
    # While down (inside cooldown) database calls fail fast.
    assert supabase_client._skip_db_calls() is True


def test_success_recovers_from_degraded():
    supabase_client._record_failure(ConnectionError("blip"))
    supabase_client._record_success()
    assert supabase_client.health_state()["state"] == "healthy"


def test_failures_outside_window_do_not_accumulate():
    supabase_client._record_failure(ConnectionError("a"))
    # Simulate the window having elapsed.
    supabase_client._HEALTH["window_started_at"] = time.time() - 120
    supabase_client._record_failure(ConnectionError("b"))
    snap = supabase_client.health_state()
    assert snap["state"] == "degraded"          # not down
    assert snap["consecutive_failures"] == 1     # window restarted


def test_down_state_fails_fast_then_allows_one_probe():
    for _ in range(3):
        supabase_client._record_failure(ConnectionError("boom"))
    assert supabase_client._skip_db_calls() is True

    # Cooldown elapsed -> exactly one probe allowed...
    supabase_client._HEALTH["down_since"] = time.time() - 31
    assert supabase_client._skip_db_calls() is False
    # ...and the probe re-arms the cooldown if it fails.
    supabase_client._record_failure(ConnectionError("still down"))
    assert supabase_client.health_state()["state"] == "down"
    assert supabase_client._skip_db_calls() is True


def test_cursor_raises_fast_while_down():
    for _ in range(3):
        supabase_client._record_failure(ConnectionError("boom"))
    t0 = time.monotonic()
    with pytest.raises(supabase_client.DatabaseUnavailable):
        with supabase_client.cursor():
            pass
    assert time.monotonic() - t0 < 0.5, "cursor() must fail fast while down"

    # The fast-fail error must classify as transient so outbox items retry.
    from delivery.errors import classify_exception
    cls = classify_exception(supabase_client.DatabaseUnavailable("down"))
    assert cls.transient is True


def test_ping_skipped_while_down():
    for _ in range(3):
        supabase_client._record_failure(ConnectionError("boom"))
    with patch.object(supabase_client, "is_configured", return_value=True):
        t0 = time.monotonic()
        assert supabase_client.ping() is False
        assert time.monotonic() - t0 < 0.5


def test_status_snapshot_includes_db_health():
    from delivery.worker import status_snapshot
    snap = status_snapshot()
    assert "db_health" in snap
    assert snap["db_health"]["state"] in ("healthy", "degraded", "down")


# ── 3. IPv6-only pooler detection ──────────────────────────────────────────

def test_direct_supabase_host_logs_ipv6_warning(caplog):
    with caplog.at_level("WARNING"):
        supabase_client._warn_if_direct_supabase_host(
            "postgresql://u:p@db.abcdefghij.supabase.co:5432/postgres"
            "?sslmode=verify-full")
    assert any("IPv6" in rec.getMessage() for rec in caplog.records)


def test_pooler_host_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        supabase_client._warn_if_direct_supabase_host(
            "postgresql://u:p@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
            "?sslmode=verify-full")
    assert not [r for r in caplog.records if "IPv6" in r.getMessage()]


# ── 4. Existing behaviour must not regress ─────────────────────────────────

def test_weaker_sslmode_still_refused(monkeypatch):
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://u:p@h:5432/db?sslmode=require")
    assert supabase_client.is_configured() is False


def test_missing_cert_still_refused_with_path_in_log(tmp_path, monkeypatch, caplog):
    missing = str(tmp_path / "nope.crt")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    monkeypatch.setenv("SUPABASE_SSL_ROOT_CERT", missing)
    with caplog.at_level("ERROR"):
        assert supabase_client.is_configured() is False
    assert any(missing in rec.getMessage() for rec in caplog.records)


def test_app_boots_with_database_down(monkeypatch):
    """The app factory must never block on or require the database."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@10.255.255.1:5432/db")
    monkeypatch.setenv("SUPABASE_CONNECT_TIMEOUT", "1")
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "x" * 32})
    with app.test_client() as c:
        r = c.get("/healthz")
        assert r.status_code == 200
