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

Optional:
   SUPABASE_SSL_ROOT_CERT=/path/to/supabase-ca.crt
   SUPABASE_CONNECT_TIMEOUT=5
   SUPABASE_STATEMENT_TIMEOUT=10
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("anvil.supabase")

# Kept as module attributes because delivery/worker.py resets them after fork().
_LOCK = threading.Lock()
_ENGINE = None

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
                _ENGINE = ConnectionPool(
                    conninfo=dsn,
                    min_size=1,
                    max_size=_int_env("SUPABASE_POOL_MAX_SIZE", 5, 1, 20),
                    timeout=_int_env("SUPABASE_POOL_WAIT_SECONDS", 10, 1, 60),
                    open=True,
                    kwargs={
                        "connect_timeout": _int_env("SUPABASE_CONNECT_TIMEOUT", 5, 1, 60),
                        "application_name": "anvil-devtools",
                        "options": f"-c statement_timeout={statement_ms}",
                        "row_factory": dict_row,
                    },
                )
    return _ENGINE


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Borrow a pooled connection, yield a cursor, commit on success.

    The pool rolls back on error and returns the connection for reuse.
    Rows come back as dicts, so use ``row["column"]``.
    """
    with _get_pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur


def ping() -> bool:
    """Return True if the database answers a trivial query. Never raises."""
    if not is_configured():
        return False
    try:
        with cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()
            return bool(row and row["ok"] == 1)
    except Exception as exc:
        log.warning("Supabase ping failed: %s", exc)
        return False


def reset_engine() -> None:
    """Close the pool. The next call opens a new one."""
    global _ENGINE
    with _LOCK:
        pool, _ENGINE = _ENGINE, None
    if pool is not None:
        pool.close()
