"""Supabase / PostgreSQL connection module.

Design notes
------------
- Uses ``psycopg`` (v3) for direct Postgres access. This gives us real TLS,
  ``connect_timeout`` and ``statement_timeout`` support, and the
  ``FOR UPDATE SKIP LOCKED`` syntax required by the durable queue.
- ``DATABASE_URL`` wins over the per-part environment variables.
- Passwords are URL-encoded with ``urllib.parse.quote`` rather than
  concatenated raw.
- A connection is opened lazily on first use and recreated on failure.
  The pool is intentionally a per-process cache of one connection because
  Supabase's free-tier Postgres limits the number of concurrent connections.
- Transient vs permanent errors are classified by :mod:`delivery.errors` so the
  retry worker can decide whether to retry, back off, or dead-letter.

Security
--------
- ``sslmode=verify-full`` is the default. Unverified modes (``require``,
  ``prefer``, ``allow``, ``disable``) are refused, not downgraded to,
  because ``require`` encrypts without checking the server certificate.
  Set ``SUPABASE_SSL_ROOT_CERT`` to Supabase's root CA file.
- The service-role key is held only in process memory and is never logged
  or shipped to the browser. Templates and API responses receive no
  Supabase credentials.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional
from urllib.parse import parse_qsl, quote as url_quote, urlencode, urlsplit, urlunsplit

import psycopg

log = logging.getLogger("anvil.supabase")

# ── Module-level state ──────────────────────────────────────────────────────

_LOCK = threading.Lock()
_ENGINE: Optional["_Engine"] = None

_DEFAULTS = {
    "SUPABASE_CONNECT_TIMEOUT": "5",
    "SUPABASE_STATEMENT_TIMEOUT": "10",
}


@dataclass(frozen=True)
class SupabaseConfig:
    """Resolved configuration derived from environment variables."""

    database_url: Optional[str]
    supabase_url: Optional[str]
    anon_key: Optional[str]
    service_role_key: Optional[str]
    connect_timeout_seconds: int
    statement_timeout_seconds: int
    enabled: bool  # True only when a usable DSN is present

    def safe_summary(self) -> dict:
        """A safe-to-log snapshot of the configuration (no secrets)."""
        return {
            "has_database_url": bool(self.database_url),
            "supabase_url": self.supabase_url or "",
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "statement_timeout_seconds": self.statement_timeout_seconds,
            "enabled": self.enabled,
        }


# sslmodes that encrypt but do NOT verify the server certificate (libpq
# documents ``require`` as encryption-only unless a root CA file is present).
# These are refused, not silently upgraded, so the operator sees the problem.
_UNVERIFIED_SSLMODES = {"disable", "allow", "prefer", "require"}
_VERIFYING_SSLMODES = {"verify-full", "verify-ca"}

_DEFAULT_SUPABASE_DB_HOST = "db.iiaalcfzqqkzxcnmnedt.supabase.co"


def _secure_dsn(dsn: str, root_cert: Optional[str]) -> Optional[str]:
    """Return ``dsn`` with certificate verification enforced, or None.

    * ``sslmode`` defaults to ``verify-full`` (certificate AND hostname).
    * An explicit unverified mode (``require``/``prefer``/``allow``/
      ``disable``) is REFUSED: the connection is treated as unconfigured
      and an error is logged. We never downgrade a request to get it to
      connect, per the production TLS requirement.
    * ``SUPABASE_SSL_ROOT_CERT`` (path to Supabase's root CA) is added as
      ``sslrootcert``. A configured path that does not exist also fails
      closed, because connecting without the intended CA would fall back
      to a different trust store.
    """
    parts = urlsplit(dsn)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    requested = query.get("sslmode", "").strip().lower()

    if requested in _UNVERIFIED_SSLMODES:
        log.error(
            "Refusing database connection: sslmode=%s does not verify the "
            "server certificate. Use sslmode=verify-full (and set "
            "SUPABASE_SSL_ROOT_CERT to Supabase's root CA).", requested,
        )
        return None
    if requested and requested not in _VERIFYING_SSLMODES:
        log.error("Refusing database connection: unknown sslmode=%r.", requested)
        return None
    if requested == "verify-ca":
        log.warning("sslmode=verify-ca does not check the hostname; verify-full is preferred.")

    query["sslmode"] = requested or "verify-full"

    if root_cert:
        if not os.path.isfile(root_cert):
            log.error(
                "SUPABASE_SSL_ROOT_CERT points to a missing file; refusing to "
                "connect without the configured CA."
            )
            return None
        query.setdefault("sslrootcert", root_cert)

    return urlunsplit(parts._replace(query=urlencode(query)))


def _build_dsn() -> Optional[str]:
    """Build a verified Postgres DSN from env vars, preferring ``DATABASE_URL``.

    Per-part variables (host, port, user, db name, password) follow the
    Supabase project layout. The password is URL-encoded so special
    characters in it cannot break the URI. Every returned DSN has
    certificate verification enforced (see :func:`_secure_dsn`).
    """
    root_cert = os.environ.get("SUPABASE_SSL_ROOT_CERT", "").strip() or None

    raw = os.environ.get("DATABASE_URL", "").strip()
    if raw:
        return _secure_dsn(raw, root_cert)

    password = os.environ.get("SUPABASE_DB_PASSWORD", "").strip()
    if not password:
        return None

    host = os.environ.get("SUPABASE_DB_HOST", "").strip() or _DEFAULT_SUPABASE_DB_HOST
    port = os.environ.get("SUPABASE_DB_PORT", "5432")
    user = os.environ.get("SUPABASE_DB_USER", "postgres")
    dbname = os.environ.get("SUPABASE_DB_NAME", "postgres")

    # URL-encode the password — it's the only part that may legally contain
    # reserved characters such as '@', ':', '/', '?', '#'.
    safe_password = url_quote(password, safe="")
    dsn = f"postgresql://{user}:{safe_password}@{host}:{port}/{dbname}"
    return _secure_dsn(dsn, root_cert)


def load_config() -> SupabaseConfig:
    """Read configuration from the environment once at startup.

    Reading on every request would be wasteful and would also mask
    configuration mistakes — we want the app to fail loudly at boot
    rather than silently 500 in production.
    """
    dsn = _build_dsn()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip() or None
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip() or None
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip() or None

    try:
        connect_timeout = int(
            os.environ.get("SUPABASE_CONNECT_TIMEOUT", _DEFAULTS["SUPABASE_CONNECT_TIMEOUT"])
        )
    except ValueError:
        connect_timeout = int(_DEFAULTS["SUPABASE_CONNECT_TIMEOUT"])
    try:
        statement_timeout = int(
            os.environ.get("SUPABASE_STATEMENT_TIMEOUT", _DEFAULTS["SUPABASE_STATEMENT_TIMEOUT"])
        )
    except ValueError:
        statement_timeout = int(_DEFAULTS["SUPABASE_STATEMENT_TIMEOUT"])

    connect_timeout = max(1, min(connect_timeout, 60))
    statement_timeout = max(1, min(statement_timeout, 300))

    enabled = bool(dsn)
    if enabled and not service_role_key:
        log.warning(
            "Supabase DSN is set but SUPABASE_SERVICE_ROLE_KEY is missing; "
            "queue delivery to RLS-protected tables will fail."
        )
    return SupabaseConfig(
        database_url=dsn,
        supabase_url=supabase_url,
        anon_key=anon_key,
        service_role_key=service_role_key,
        connect_timeout_seconds=connect_timeout,
        statement_timeout_seconds=statement_timeout,
        enabled=enabled,
    )


class _Engine:
    """Per-process cached Postgres connection.

    psycopg 3 connections are thread-safe for sequential use but not for
    concurrent use from multiple threads. We guard every use with a module
    lock; gunicorn workers are single-threaded by default anyway.

    The connection is opened lazily so the app can boot in environments
    where Supabase is paused (the durable queue accepts the request and
    the worker retries until Supabase wakes up).
    """

    def __init__(self, config: SupabaseConfig) -> None:
        self._config = config
        self._conn: Optional[psycopg.Connection] = None
        self._lock = threading.Lock()

    def _raw_connect(self) -> psycopg.Connection:
        # statement_timeout is set at the server level so a runaway query
        # cannot pin a worker indefinitely even if the application never
        # gets to read its socket.
        options = f"-c statement_timeout={self._config.statement_timeout_seconds * 1000}"
        return psycopg.connect(
            self._config.database_url,
            connect_timeout=self._config.connect_timeout_seconds,
            application_name="anvil-devtools",
            options=options,
        )

    def _ensure_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._raw_connect()
        return self._conn

    def reset(self) -> None:
        """Drop the cached connection. The next call will reopen it.

        Called by the worker after a fatal error so we never get stuck
        reusing a half-broken socket.
        """
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @contextmanager
    def cursor(self) -> Iterator[psycopg.Cursor]:
        """Yield a cursor, restoring the connection to a clean state on exit.

        On any error the connection is closed so the next call reopens it
        from scratch — Postgres connections in an unclean state can return
        subtly wrong results.

        The cursor uses ``RealDictCursor`` row factory so callers can
        refer to columns by name rather than fragile positional indices.
        """
        if not self._config.enabled:
            raise RuntimeError("Supabase is not configured (DATABASE_URL missing).")
        with self._lock:
            conn = self._ensure_conn()
            try:
                cur = conn.cursor(row_factory=psycopg.rows.dict_row)
                try:
                    yield cur
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    cur.close()
            except Exception:
                # Connection is suspect. Drop it so we don't reuse a
                # half-broken socket on the next call.
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                raise


def get_engine() -> _Engine:
    """Return the per-process cached engine, initializing it lazily."""
    global _ENGINE
    if _ENGINE is None:
        with _LOCK:
            if _ENGINE is None:
                _ENGINE = _Engine(load_config())
    return _ENGINE


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """Module-level shortcut used by the rest of the app."""
    yield from get_engine().cursor()


def reset_engine() -> None:
    """Drop the cached connection. Used by tests and the worker."""
    eng = _ENGINE
    if eng is not None:
        eng.reset()


def is_configured() -> bool:
    """True when a usable DSN is available."""
    return load_config().enabled


def ping() -> bool:
    """Lightweight connectivity probe used by /admin/queue and /healthz.

    Returns True on success, False on any failure. Never raises so it can
    be safely called from a request handler without taking the whole
    request down.
    """
    if not is_configured():
        return False
    try:
        with cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
            return bool(row and row[0] == 1)
    except Exception as exc:
        log.info("Supabase ping failed: %s", exc)
        return False


# ── Service-role header helper ───────────────────────────────────────────────

def service_role_header(self_unused=None) -> dict:
    """Return the headers needed to call Supabase REST with the service-role key.

    Returned only to other server-side modules; never serialized to a
    template or response. If the key is unset, an empty mapping is
    returned so callers fail loud at the actual API call rather than
    silently sending the wrong Authorization header.
    """
    cfg = load_config()
    if cfg.service_role_key:
        return {
            "apikey": cfg.service_role_key,
            "Authorization": f"Bearer {cfg.service_role_key}",
        }
    return {}
