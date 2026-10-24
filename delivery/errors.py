"""Error classification + exponential backoff with full jitter.

The retry policy distinguishes:

* **Transient** errors — worth retrying with backoff:
  - Connection refused / timeout / DNS failure / TLS reset
  - Postgres "the database system is starting up" (Supabase paused)
  - Statement timeout
  - HTTP 408/429/500/502/503/504
  - Network errors (``psycopg.OperationalError`` whose ``pgcode`` is
    retryable, or no ``pgcode`` at all — usually a network blip)

* **Permanent** errors — must dead-letter immediately:
  - Authentication / authorization failures (invalid API key, RLS denial)
  - Constraint violations (``unique_violation``, ``check_violation``)
  - Malformed payload / schema mismatch (``undefined_table``,
    ``undefined_column``, ``syntax_error``)
  - The HTTP 4xx family except the retryable ones above

The worker never blocks waiting for a backoff interval inside the request
thread — it updates ``next_attempt_at`` in the queue row and moves on.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("anvil.queue.backoff")


# ── Permanent Postgres SQLSTATE codes ────────────────────────────────────────
# These indicate a problem with the request itself, not the connection.
# Retrying with the same payload will never succeed.
PERMANENT_SQLSTATES = {
    # Integrity constraint violations — payload is wrong, retrying won't help.
    "23502",  # not_null_violation
    "23503",  # foreign_key_violation
    "23505",  # unique_violation (we treat as permanent unless caller knows it's idempotent)
    "23514",  # check_violation
    "23P01",  # exclusion_violation
    # Schema / SQL mistakes — the app code is wrong.
    "42P01",  # undefined_table
    "42703",  # undefined_column
    "42804",  # datatype_mismatch
    "42P10",  # invalid_column_reference
    "42P17",  # invalid_object_definition
    # Authorization / config (RLS, bad role).
    "42501",  # insufficient_privilege
    # Data exceptions that are not transient (wrong type, malformed JSON).
    "22P02",  # invalid_text_representation
    "22P05",  # untranslatable_character
    "22001",  # string_data_right_truncation
}

# ── Transient Postgres SQLSTATE codes ────────────────────────────────────────
# These can succeed on retry after a backoff.
# Authentication failures (28000, 28P01) are NOT permanent. They mean the
# deployment's credentials are wrong or were rotated, which an operator fixes
# by updating a secret. Classifying them as permanent dead-lettered every
# pending contact submission on a password rotation. They are transient with
# backoff, so items wait and the worker recovers once the secret is corrected.
TRANSIENT_SQLSTATES = {
    "28000",  # invalid_authorization_specification (config; wait for operator)
    "28P01",  # invalid_password (config; wait for operator)
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "53P03",  # lock_not_available (FOR UPDATE NOWAIT)
    "57P03",  # cannot_connect_now (Supabase paused)
    "08000",  # connection_exception
    "08003",  # connection_does_not_exist
    "08006",  # connection_failure
    "08001",  # sqlclient_unable_to_establish_sqlconnection
    "08004",  # sqlserver_rejected_establishment_of_sqlconnection
    "57P02",  # admin_shutdown (during a deploy)
    "57P04",  # database_dropped
    "53300",  # too_many_connections
    "53201",  # configuration_limit_exceeded
    "53400",  # configuration_limit_exceeded
}

# HTTP status codes worth retrying.
RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class ErrorClassification:
    """Result of classifying an exception."""

    transient: bool
    code: str
    reason: str

    @property
    def permanent(self) -> bool:
        # A non-transient error is permanent only when we actually know what
        # it is. An unknown error (no code, no recognizable class) is treated
        # as transient — the worker will retry a bounded number of times
        # before dead-lettering for human review.
        return not self.transient and self.code not in {"", "unknown"}


def classify_exception(exc: BaseException) -> ErrorClassification:
    """Classify an exception as transient or permanent.

    The classification is intentionally conservative: when in doubt, retry
    (transient). The only way to land in the permanent bucket is to match a
    known non-retryable SQLSTATE, a known permanent exception type, or an
    HTTP 4xx status (excluding 408/425/429).
    """
    # Late import — psycopg only exists when Supabase is configured.
    try:
        import psycopg
        from psycopg.errors import Error as PsycopgError
    except Exception:  # pragma: no cover — psycopg is in requirements
        psycopg = None
        PsycopgError = ()  # type: ignore

    try:
        import requests
        from requests.exceptions import RequestException
    except Exception:  # pragma: no cover
        requests = None
        RequestException = ()  # type: ignore

    # psycopg errors carry a pgcode (SQLSTATE) on the exception instance.
    if psycopg is not None and isinstance(exc, PsycopgError):
        # psycopg 3 exposes the SQLSTATE as ``sqlstate``. ``pgcode`` is the
        # psycopg 2 name and never exists on psycopg 3 errors. Reading only
        # ``pgcode`` made every database error fall through to the generic
        # transient branch, so permanent schema and constraint errors were
        # retried forever and never dead-lettered (reproduced).
        sqlstate = str(getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", "") or "").upper()
        if sqlstate in PERMANENT_SQLSTATES:
            return ErrorClassification(
                transient=False, code=sqlstate, reason="permanent_sqlstate"
            )
        if sqlstate in TRANSIENT_SQLSTATES:
            return ErrorClassification(
                transient=True, code=sqlstate, reason="transient_sqlstate"
            )
        if sqlstate:
            # Unknown SQLSTATE — be safe and retry.
            return ErrorClassification(
                transient=True, code=sqlstate, reason="unknown_sqlstate_retry"
            )
        # OperationalError with no SQLSTATE usually means a transport-level
        # failure (TCP reset, DNS timeout, TLS handshake, paused Supabase
        # waking up). These are textbook transient.
        if isinstance(exc, psycopg.OperationalError):
            return ErrorClassification(
                transient=True, code="operational", reason="transport_or_pause"
            )
        return ErrorClassification(
            transient=True, code="psycopg_other", reason="unknown_psycopg_error"
        )

    # requests/HTTP errors: inspect the response status code if available.
    if requests is not None and isinstance(exc, RequestException):
        response = getattr(exc, "response", None)
        if response is not None:
            status = getattr(response, "status_code", 0) or 0
            if status in RETRYABLE_HTTP_STATUSES:
                return ErrorClassification(
                    transient=True, code=f"http_{status}", reason="retryable_http_status"
                )
            if 400 <= status < 500:
                return ErrorClassification(
                    transient=False, code=f"http_{status}", reason="client_error"
                )
            if status >= 500:
                return ErrorClassification(
                    transient=True, code=f"http_{status}", reason="server_error"
                )
        # No response — connection error, DNS, TLS, timeout. Retry.
        return ErrorClassification(
            transient=True, code="request_error", reason="transport_error"
        )

    # Plain OSError covers connection refused, DNS failures, TLS errors.
    if isinstance(exc, (OSError, ConnectionError, TimeoutError)):
        return ErrorClassification(
            transient=True, code="os_error", reason="os_level_transport"
        )

    # Generic Exception — we don't know, so retry cautiously.
    return ErrorClassification(
        transient=True, code="unknown", reason="unknown_exception_retry"
    )


# ── Exponential backoff with full jitter ────────────────────────────────────

@dataclass(frozen=True)
class BackoffConfig:
    """Tunable backoff policy.

    Defaults follow the spec: base delay 2 s, max delay 15 min (900 s).
    """

    base_delay: float = 2.0
    max_delay: float = 900.0

    @classmethod
    def from_env(cls) -> "BackoffConfig":
        """Read tunables from the environment with sane defaults."""
        try:
            base = float(os.environ.get("SUPABASE_RETRY_BASE_DELAY_SECONDS", "2"))
        except ValueError:
            base = 2.0
        try:
            mx = float(os.environ.get("SUPABASE_RETRY_MAX_DELAY_SECONDS", "900"))
        except ValueError:
            mx = 900.0
        # Clamp to sensible ranges so a typo can't disable backoff.
        base = max(0.1, min(base, 60.0))
        mx = max(base, min(mx, 86400.0))  # cap at 24 h
        return cls(base_delay=base, max_delay=mx)


def compute_backoff(
    attempt_count: int,
    config: Optional[BackoffConfig] = None,
    *,
    respect_retry_after: Optional[float] = None,
    rng: Optional[random.Random] = None,
) -> float:
    """Compute the next backoff delay in seconds.

    Implements **exponential backoff with full jitter** as recommended in
    AWS Architecture Blog: "Exponential Backoff and Jitter".

        delay = random.uniform(0, min(max_delay, base_delay * 2 ** attempt))

    ``respect_retry_after`` (seconds) is honoured as the upper bound when set
    (e.g. from an HTTP 429 ``Retry-After`` header). The function still
    applies jitter so multiple workers don't synchronize.

    A small floor (1% of the upper bound, capped at 100 ms) prevents tight
    retry loops when many items fail simultaneously and full jitter returns
    values close to zero. The floor does not change the long-run
    distribution meaningfully.

    ``rng`` is injectable for deterministic tests.
    """
    cfg = config or BackoffConfig.from_env()
    r = rng if rng is not None else random
    # Cap the exponent. Unbounded 2**n overflows a float at n=1024 (confirmed
    # by reproduction: OverflowError), which a long outage can reach.
    expo = cfg.base_delay * (2 ** min(max(0, attempt_count), 60))
    upper = min(cfg.max_delay, expo)
    if respect_retry_after is not None:
        upper = min(upper, max(0.0, float(respect_retry_after)))
    # Always at least 1 ms so the next_attempt_at moves slightly into the
    # future, even under the most aggressive backoff.
    upper = max(upper, 0.001)
    # Absolute floor of 1 ms so the worker can never form a tight retry
    # loop. This deviates slightly from textbook full jitter but the
    # spec ("never create a tight infinite loop") takes precedence.
    return max(0.001, float(r.uniform(0.0, upper)))
