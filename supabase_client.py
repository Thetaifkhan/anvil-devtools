"""Simple Supabase (Postgres) connection helper.

How it works
------------
* Build one connection string (DSN) from environment variables.
* Open a fresh connection for each piece of work, and close it when done.
  No caching, threads or locks to get wrong.
* TLS is always verified (``sslmode=verify-full``). Weaker modes are refused.

Environment variables (use ONE of the two options)
--------------------------------------------------
1. DATABASE_URL=postgresql://user:password@host:5432/postgres
   (password must be URL-encoded)
2. SUPABASE_DB_PASSWORD=...  plus optional SUPABASE_DB_HOST, SUPABASE_DB_USER,
   SUPABASE_DB_NAME, SUPABASE_DB_PORT

On Render, use the Supabase **Session pooler** host
(``aws-0-<region>.pooler.supabase.com``). The direct host
(``db.<project-ref>.supabase.co``) resolves to IPv6 only, which Render's
free instances cannot reach. The module detects a direct host and logs a
warning.

Optional:
   SUPABASE_SSL_ROOT_CERT=/path/to/supabase-ca.crt
   SUPABASE_CONNECT_TIMEOUT=5
   SUPABASE_STATEMENT_TIMEOUT=10
   SUPABASE_POOL_MAX_SIZE=5
   SUPABASE_POOL_WAIT_SECONDS=10
   SUPABASE_POOL_MAX_IDLE=60        (seconds before an idle conn is recycled)
   SUPABASE_POOL_MAX_LIFETIME=1800  (30 min; stale conns are replaced)

Health state machine
--------------------
``healthy`` -> ``degraded`` (one failure) -> ``down`` (3 failures within
60 s). While ``down``, database calls are skipped for 30 s (they fail fast
instead of waiting for the connect timeout), then a single probe is allowed.
``health_state()`` exposes the snapshot for the admin dashboard.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger("anvil.supabase")

# Kept as module attributes because delivery/worker.py resets them after fork().
_LOCK = threading.Lock()
_ENGINE = None


class DatabaseUnavailable(RuntimeError):
    """Raised immediately while the health machine is in the 'down' state.

    Classified as transient by delivery.errors.classify_exception, so outbox
    items are retried with backoff instead of dead-lettered.
    """


# ── Health state machine ─────────────────────────────────────────────────────
# healthy -> degraded (1 failure) -> down (3 failures within 60 s).
# 'down' skips database calls for 30 s, then allows a single probe.

_HEALTH_LOCK = threading.Lock()
_HEALTH = {
    "state": "healthy",
    "consecutive_failures": 0,
    "window_started_at": 0.0,
    "down_since": 0.0,
    "last_success_at": 0.0,
    "last_failure_at": 0.0,
    "last_error": "",
}

_FAILURES_FOR_DOWN = 3
_FAILURE_WINDOW_SECONDS = 60.0
_DOWN_COOLDOWN_SECONDS = 30.0


def health_state() -> dict:
    """Snapshot of the connection health machine (for the admin UI)."""
    with _HEALTH_LOCK:
        snap = dict(_HEALTH)
    now = time.time()
    if snap["state"] == "down":
        snap["cooldown_remaining_seconds"] = max(
            0.0, _DOWN_COOLDOWN_SECONDS - (now - snap["down_since"]))
    return snap


def _record_success() -> None:
    with _HEALTH_LOCK:
        _HEALTH["state"] = "healthy"
        _HEALTH["consecutive_failures"] = 0
        _HEALTH["window_started_at"] = 0.0
        _HEALTH["last_success_at"] = time.time()


def _record_failure(exc: BaseException) -> None:
    now = time.time()
    with _HEALTH_LOCK:
        _HEALTH["consecutive_failures"] += 1
        _HEALTH["last_failure_at"] = now
        _HEALTH["last_error"] = str(exc)[:200]
        if _HEALTH["state"] == "healthy":
            _HEALTH["state"] = "degraded"
        if _HEALTH["window_started_at"] == 0.0:
            _HEALTH["window_started_at"] = now
        within_window = now - _HEALTH["window_started_at"] <= _FAILURE_WINDOW_SECONDS
        if (within_window
                and _HEALTH["consecutive_failures"] >= _FAILURES_FOR_DOWN):
            _HEALTH["state"] = "down"
            _HEALTH["down_since"] = now
        elif not within_window:
            # Failures are spread out: restart the window instead of
            # accumulating toward 'down' forever.
            _HEALTH["window_started_at"] = now
            _HEALTH["consecutive_failures"] = 1


def _skip_db_calls() -> bool:
    """True while the machine is 'down' and inside its cooldown.

    After the cooldown elapses this returns False exactly once for the next
    call (the probe); its outcome moves the machine back to healthy or
    re-arms the cooldown.
    """
    with _HEALTH_LOCK:
        if _HEALTH["state"] != "down":
            return False
        elapsed = time.time() - _HEALTH["down_since"]
        if elapsed >= _DOWN_COOLDOWN_SECONDS:
            # Allow one probe; a failure will re-arm down_since.
            _HEALTH["down_since"] = time.time()
            return False
        return True


def reset_health() -> None:
    """Reset the health machine (used by tests and after fork)."""
    with _HEALTH_LOCK:
        _HEALTH.update({
            "state": "healthy",
            "consecutive_failures": 0,
            "window_started_at": 0.0,
            "down_since": 0.0,
            "last_success_at": 0.0,
            "last_failure_at": 0.0,
            "last_error": "",
        })

_DEFAULT_HOST = "db.iiaalcfzqqkzxcnmnedt.supabase.co"
_VERIFY_MODE = "verify-full"
_REFUSED_SSLMODES = {"disable", "allow", "prefer", "require", "verify-ca"}


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except ValueError:
        value = default
    return max(low, min(value, high))


def _secure_dsn(dsn: str, root_cert: Optional[str]) -> Optional[str]:
    """Force certificate verification on a DSN. Returns None if it must be refused."""
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    requested = query.get("sslmode", "").strip().lower()

    if requested and requested != _VERIFY_MODE:
        log.error(
            "Database connection refused: sslmode=%s does not fully verify the "
            "server. Use sslmode=verify-full.", requested,
        )
        return None

    query["sslmode"] = _VERIFY_MODE
    if root_cert:
        if not os.path.isfile(root_cert):
            log.error(
                "Database connection refused: SUPABASE_SSL_ROOT_CERT=%s does not "
                "exist. Upload the Supabase CA certificate to that path (on Render: "
                "Secret Files).", root_cert,
            )
            return None
        query.setdefault("sslrootcert", root_cert)

    return urlunsplit(parts._replace(query=urlencode(query)))


def _build_dsn() -> Optional[str]:
    """Return a verified DSN from the environment, or None if not configured."""
    root_cert = os.environ.get("SUPABASE_SSL_ROOT_CERT", "").strip() or None

    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw:
        return _secure_dsn(raw, root_cert)

    password = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
    if not password:
        return None

    host = os.environ.get("SUPABASE_DB_HOST", "").strip() or _DEFAULT_HOST
    port = os.environ.get("SUPABASE_DB_PORT", "5432").strip()
    user = os.environ.get("SUPABASE_DB_USER", "postgres").strip()
    dbname = os.environ.get("SUPABASE_DB_NAME", "postgres").strip()

    dsn = f"postgresql://{user}:{quote(password, safe='')}@{host}:{port}/{dbname}"
    return _secure_dsn(dsn, root_cert)


_DIRECT_HOST_RE = None  # compiled lazily to keep the module import cheap


def _warn_if_direct_supabase_host(dsn: str) -> None:
    """db.<ref>.supabase.co is IPv6-only and unreachable from Render free
    instances. Detect it and point the operator at the Session pooler."""
    global _DIRECT_HOST_RE
    import re
    if _DIRECT_HOST_RE is None:
        _DIRECT_HOST_RE = re.compile(r"@db\.[a-z0-9]+\.supabase\.co(:|$)")
    if _DIRECT_HOST_RE.search(dsn):
        log.warning(
            "DATABASE_URL uses the direct host db.<ref>.supabase.co, which is "
            "IPv6-only. Render free instances have no IPv6 egress, so the "
            "connection will fail. Use the Session pooler host instead: "
            "aws-0-<region>.pooler.supabase.com (port 5432)."
        )


def is_configured() -> bool:
    """True when a usable, verified DSN can be built from the environment."""
    return _build_dsn() is not None


def _get_pool() -> ConnectionPool:
    """Create the shared connection pool on first use (once per process)."""
    global _ENGINE
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                dsn = _build_dsn()
                if dsn is None:
                    raise RuntimeError(
                        "Supabase is not configured. Set DATABASE_URL (or SUPABASE_DB_PASSWORD) "
                        "and check the TLS settings in the logs."
                    )
                statement_ms = _int_env("SUPABASE_STATEMENT_TIMEOUT", 10, 1, 300) * 1000
                _warn_if_direct_supabase_host(dsn)
                _ENGINE = ConnectionPool(
                    conninfo=dsn,
                    min_size=1,
                    max_size=_int_env("SUPABASE_POOL_MAX_SIZE", 5, 1, 20),
                    timeout=_int_env("SUPABASE_POOL_WAIT_SECONDS", 10, 1, 60),
                    # Recycle connections so a stale socket or a server-side
                    # reset never pins a broken connection in the pool.
                    max_idle=_int_env("SUPABASE_POOL_MAX_IDLE", 60, 1, 3600),
                    max_lifetime=_int_env("SUPABASE_POOL_MAX_LIFETIME", 1800, 60, 86400),
                    open=True,
                    kwargs={
                        "connect_timeout": _int_env("SUPABASE_CONNECT_TIMEOUT", 5, 1, 60),
                        "application_name": "anvil-devtools",
                        "options": f"-c statement_timeout={statement_ms}",
                    },
                )
    return _ENGINE


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Borrow a pooled connection, yield a cursor, commit on success.

    The pool rolls back on error and returns the connection for reuse.
    Rows come back as tuples, so use ``row[0]``, ``row[1]`` and so on.

    Health-gated: while the machine is 'down' (and inside its cooldown)
    this raises :class:`DatabaseUnavailable` immediately instead of waiting
    for the connect timeout, so a dead database never pins the request
    thread or the worker loop.
    """
    if _skip_db_calls():
        raise DatabaseUnavailable(
            "Database is marked down by the health check; skipping this "
            "call for the cooldown period."
        )
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                yield cur
    except BaseException as exc:
        _record_failure(exc)
        raise
    _record_success()


def ping() -> bool:
    """Return True if the database answers a trivial query. Never raises."""
    if not is_configured():
        return False
    if _skip_db_calls():
        log.info("Supabase ping skipped: health machine is in cooldown.")
        return False
    try:
        with _get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
                ok = bool(row and row[0] == 1)
        if ok:
            _record_success()
        return ok
    except Exception as exc:
        _record_failure(exc)
        log.warning("Supabase ping failed: %s", exc)
        return False


def reset_engine() -> None:
    """Close the pool. The next call opens a new one."""
    global _ENGINE
    with _LOCK:
        pool, _ENGINE = _ENGINE, None
    if pool is not None:
        pool.close()
