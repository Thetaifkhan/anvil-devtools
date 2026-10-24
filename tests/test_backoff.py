"""Tests for backoff computation + error classification."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery.errors import (
    BackoffConfig,
    ErrorClassification,
    classify_exception,
    compute_backoff,
    PERMANENT_SQLSTATES,
    TRANSIENT_SQLSTATES,
)


# ── Error classification ──────────────────────────────────────────────────

class TestClassifyException:
    def test_os_error_is_transient(self):
        cls = classify_exception(OSError("connection refused"))
        assert cls.transient is True

    def test_connection_refused_is_transient(self):
        cls = classify_exception(ConnectionError("refused"))
        assert cls.transient is True

    def test_timeout_error_is_transient(self):
        cls = classify_exception(TimeoutError("read timeout"))
        assert cls.transient is True

    def test_unknown_exception_is_transient_by_default(self):
        cls = classify_exception(RuntimeError("unexpected"))
        # Unknown errors are conservative — retry rather than dead-letter.
        assert cls.transient is True

    def test_requests_connection_error_is_transient(self):
        import requests
        cls = classify_exception(requests.ConnectionError("network down"))
        assert cls.transient is True

    def test_requests_429_is_transient(self):
        import requests
        resp = requests.Response()
        resp.status_code = 429
        exc = requests.exceptions.HTTPError("429 Too Many Requests", response=resp)
        cls = classify_exception(exc)
        assert cls.transient is True

    def test_requests_400_is_permanent(self):
        import requests
        resp = requests.Response()
        resp.status_code = 400
        exc = requests.exceptions.HTTPError("400 Bad Request", response=resp)
        cls = classify_exception(exc)
        assert cls.transient is False
        assert cls.permanent is True

    def test_requests_401_is_permanent(self):
        import requests
        resp = requests.Response()
        resp.status_code = 401
        exc = requests.exceptions.HTTPError("401 Unauthorized", response=resp)
        cls = classify_exception(exc)
        assert cls.transient is False

    def test_requests_500_is_transient(self):
        import requests
        resp = requests.Response()
        resp.status_code = 500
        exc = requests.exceptions.HTTPError("500 Internal Server Error", response=resp)
        cls = classify_exception(exc)
        assert cls.transient is True

    def test_psycopg_operational_error_is_transient(self):
        # psycopg.OperationalError without a pgcode usually means a
        # transport failure (TCP reset, DNS, Supabase paused).
        import psycopg
        exc = psycopg.OperationalError("connection to server lost")
        cls = classify_exception(exc)
        assert cls.transient is True

    def test_psycopg_unique_violation_is_permanent(self):
        # 23505 = unique_violation. The app must handle these
        # explicitly via ON CONFLICT; an unexpected one is a bug.
        import psycopg
        exc = psycopg.errors.UniqueViolation()
        exc.pgcode = "23505"
        cls = classify_exception(exc)
        assert cls.transient is False
        assert cls.permanent is True

    def test_psycopg_check_violation_is_permanent(self):
        import psycopg
        exc = psycopg.errors.CheckViolation()
        exc.pgcode = "23514"
        cls = classify_exception(exc)
        assert cls.transient is False

    def test_psycopg_serialization_failure_is_transient(self):
        # 40001 = serialization_failure — classic retry.
        import psycopg
        exc = psycopg.errors.SerializationFailure()
        exc.pgcode = "40001"
        cls = classify_exception(exc)
        assert cls.transient is True

    def test_psycopg_admin_shutdown_is_transient(self):
        # 57P02 = admin_shutdown (during a deploy).
        import psycopg
        exc = psycopg.errors.OperationalError()
        exc.pgcode = "57P02"
        cls = classify_exception(exc)
        assert cls.transient is True

    def test_psycopg_insufficient_privilege_is_permanent(self):
        # 42501 = insufficient_privilege — retrying won't help.
        import psycopg
        exc = psycopg.errors.InsufficientPrivilege()
        exc.pgcode = "42501"
        cls = classify_exception(exc)
        assert cls.transient is False


# ── Backoff computation ───────────────────────────────────────────────────

class TestBackoff:
    def test_first_attempt_delay_is_bounded_by_base(self):
        cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
        rng = random.Random(0)
        for _ in range(100):
            d = compute_backoff(0, cfg, rng=rng)
            assert 0 <= d <= 2.0

    def test_backoff_grows_exponentially(self):
        cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
        rng = random.Random(42)
        delays = [compute_backoff(n, cfg, rng=rng) for n in range(8)]
        upper_bounds = [min(900.0, 2.0 * (2 ** n)) for n in range(8)]
        for d, ub in zip(delays, upper_bounds):
            assert 0 <= d <= ub

    def test_backoff_caps_at_max_delay(self):
        cfg = BackoffConfig(base_delay=2.0, max_delay=10.0)
        rng = random.Random(0)
        for _ in range(100):
            d = compute_backoff(50, cfg, rng=rng)  # attempt 50 would be 2 * 2^50
            assert d <= 10.0

    def test_backoff_uses_full_jitter(self):
        """Full jitter: delay should be uniformly distributed in [0, upper]."""
        cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
        rng = random.Random(0)
        samples = [compute_backoff(2, cfg, rng=rng) for _ in range(1000)]
        # Mean of full-jitter should be approximately upper/2.
        # Upper at attempt 2: 2 * 2^2 = 8.
        # So mean should be ~4 ± a wide tolerance.
        mean = sum(samples) / len(samples)
        assert 3.0 < mean < 5.0

    def test_retry_after_is_honoured(self):
        cfg = BackoffConfig(base_delay=2.0, max_delay=900.0)
        rng = random.Random(0)
        # If the server says "wait 1 second", the computed delay must be
        # at most 1 second (with full jitter on top).
        for _ in range(100):
            d = compute_backoff(10, cfg, rng=rng, respect_retry_after=1.0)
            assert d <= 1.0

    def test_backoff_from_env_overrides_defaults(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_RETRY_BASE_DELAY_SECONDS", "0.5")
        monkeypatch.setenv("SUPABASE_RETRY_MAX_DELAY_SECONDS", "30")
        cfg = BackoffConfig.from_env()
        assert cfg.base_delay == 0.5
        assert cfg.max_delay == 30.0

    def test_backoff_env_invalid_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_RETRY_BASE_DELAY_SECONDS", "not-a-number")
        cfg = BackoffConfig.from_env()
        assert cfg.base_delay == 2.0  # default

    def test_backoff_never_returns_zero(self):
        """A 0 delay would cause a tight retry loop. Always at least 1 ms."""
        cfg = BackoffConfig(base_delay=0.01, max_delay=1.0)
        rng = random.Random(0)
        for _ in range(100):
            d = compute_backoff(0, cfg, rng=rng)
            assert d >= 0.001
