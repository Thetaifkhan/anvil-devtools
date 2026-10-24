"""Durable outbox queue.

Persistence model
-----------------
SQLite is used because it ships with Python, needs no external service,
and survives process restarts as long as the file lives on a volume
that survives the restart.

Honest durability contract
---------------------------
This module DOES NOT claim durability on its own. The operator must
explicitly opt in by setting ``OUTBOX_DURABLE=1`` AFTER verifying that
``SUPABASE_QUEUE_PATH`` lives on a persistent volume (Render paid disk,
EBS, persistent GCP/Azure disk, etc.). Without that opt-in, the queue
is treated as **best-effort**: items survive gunicorn worker restarts
within the same instance but are LOST on instance replacement.

Routes that accept user-facing submissions (the contact form) read
``is_durable()`` and adjust their HTTP response message accordingly so
the user is never falsely told a submission was "saved" when it lives
only in a volatile queue.

Concurrency
-----------
SQLite handles concurrent writes via WAL mode + a busy_timeout. The
Flask app and the background worker share one file; writes are short
and serialized through a process-level lock to be defensive.

State machine
-------------
Each row's lifecycle:

    pending → in_flight → delivered
                  ↘ retrying → in_flight → ...
                                  ↘ dead_lettered (terminal)

The ``event_key`` column is the idempotency key. Inserts with a
duplicate ``event_key`` are no-ops so retries of the same logical
event never produce duplicate writes downstream.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

log = logging.getLogger("anvil.delivery.outbox")

# ── Constants ────────────────────────────────────────────────────────────────

# Default to /tmp on the assumption that this is the only writable path on a
# minimal container host. The operator MUST override this to a persistent
# volume path AND set OUTBOX_DURABLE=1 before claiming durability.
DEFAULT_QUEUE_PATH = "/tmp/anvil-outbox.sqlite3"
DEFAULT_MAX_ITEMS = 10_000

# Default analytics-event pruning age in DAYS.
#
# The owner's preference is long-term retention. Default to 0 (= pruning
# DISABLED). The operator may opt in to pruning by setting
# SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS to a positive integer. Set to 0
# explicitly to disable (which is also the default).
#
# Contact submissions are NEVER pruned regardless of this setting — there
# is no env var that disables this guarantee.
DEFAULT_ANALYTICS_MAX_AGE_DAYS = 0

# When the queue's pending count reaches this percentage of
# SUPABASE_MAX_QUEUE_ITEMS, the worker logs a WARNING and the
# /admin/queue dashboard surfaces a saturation alert. Set to 0 to
# disable the alert.
DEFAULT_SATURATION_ALERT_PCT = 80

# How many minutes between saturation alerts (avoids log spam).
DEFAULT_SATURATION_ALERT_COOLDOWN_MIN = 15

# Contact submissions are NEVER auto-pruned. There is no knob to disable
# this guarantee — it is a hard contract.

VALID_STATUSES = ("pending", "in_flight", "delivered", "dead_lettered")
VALID_TYPES = ("analytics_event", "contact_submission", "admin_audit")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    next_attempt_at REAL NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_at REAL,
    last_error_msg TEXT,
    delivered_at REAL,
    dead_lettered_at REAL,
    dead_letter_reason TEXT,
    lease_owner TEXT,
    lease_expires_at REAL
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox (next_attempt_at)
    WHERE delivered_at IS NULL AND dead_lettered_at IS NULL;
CREATE INDEX IF NOT EXISTS outbox_dead_letter_idx
    ON outbox (dead_lettered_at DESC)
    WHERE dead_lettered_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_event_type_idx ON outbox (event_type);
CREATE INDEX IF NOT EXISTS outbox_contact_pending_idx
    ON outbox (created_at)
    WHERE event_type = 'contact_submission'
      AND delivered_at IS NULL
      AND dead_lettered_at IS NULL;
"""

# ── Module state ─────────────────────────────────────────────────────────────

_LOCK = threading.Lock()
_DB_PATH: Optional[str] = None
_DURABILITY_VERIFIED = False  # set True by configure(durable=True)


def configure(path: Optional[str] = None, *, durable: Optional[bool] = None) -> str:
    """Set the SQLite path used by all subsequent operations.

    Must be called once at app startup. If never called, the module
    falls back to ``SUPABASE_QUEUE_PATH`` env var, then the default
    ``/tmp/anvil-outbox.sqlite3``.

    ``durable`` controls the honesty contract. If ``None``, durability
    is inferred from the ``OUTBOX_DURABLE`` env var (default: ``0``,
    i.e. best-effort). Set ``OUTBOX_DURABLE=1`` ONLY after verifying
    that ``SUPABASE_QUEUE_PATH`` lives on a persistent volume.
    """
    global _DB_PATH, _DURABILITY_VERIFIED
    resolved = (
        path
        or os.environ.get("SUPABASE_QUEUE_PATH", "").strip()
        or DEFAULT_QUEUE_PATH
    )
    if durable is None:
        durable = os.environ.get("OUTBOX_DURABLE", "0").strip().lower() in (
            "1", "true", "yes", "on"
        )
    with _LOCK:
        _DB_PATH = resolved
        _DURABILITY_VERIFIED = bool(durable)
    _init_db(resolved)
    if not _DURABILITY_VERIFIED:
        log.warning(
            "Outbox path %s is configured in BEST-EFFORT mode "
            "(OUTBOX_DURABLE != 1). Pending contact submissions will be "
            "LOST on instance replacement. Set OUTBOX_DURABLE=1 only "
            "after mounting a persistent disk at this path.",
            resolved,
        )
    else:
        log.info("Outbox path %s configured as durable.", resolved)
    return resolved


def get_db_path() -> str:
    if _DB_PATH is None:
        return configure()
    return _DB_PATH


def get_db_path_safe() -> Optional[str]:
    """Return the configured path WITHOUT auto-configuring.

    Used by app startup to decide whether to call configure() or skip it
    (tests configure the path BEFORE create_app()). Auto-configuring
    here would defeat the test override.
    """
    return _DB_PATH


def is_durable() -> bool:
    """True only if the operator has explicitly opted in to durability.

    Routes that accept user-facing submissions MUST check this and
    adjust their success message accordingly. Never tell the user a
    submission was "saved" when ``is_durable()`` returns False.
    """
    if _DB_PATH is None:
        # Module not yet configured. Lazily configure with defaults
        # (which sets _DURABILITY_VERIFIED based on the env var).
        configure()
    return _DURABILITY_VERIFIED


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a SQLite connection with the tunables we depend on.

    WAL + a 5-second busy_timeout lets the worker and the request
    thread share the file without ``database is locked`` errors under
    load.
    """
    target = path or get_db_path()
    conn = sqlite3.connect(target, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _init_db(path: str) -> None:
    """Create the schema if the file is fresh, and upgrade older files.

    Files created before leases existed lack ``lease_owner`` and
    ``lease_expires_at``. Those columns are added in place so existing
    queued items are preserved.
    """
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(outbox)").fetchall()}
        if "lease_owner" not in cols:
            conn.execute("ALTER TABLE outbox ADD COLUMN lease_owner TEXT")
        if "lease_expires_at" not in cols:
            conn.execute("ALTER TABLE outbox ADD COLUMN lease_expires_at REAL")
    finally:
        conn.close()


@contextmanager
def _cursor() -> Iterator[sqlite3.Cursor]:
    conn = _connect()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Public dataclasses ──────────────────────────────────────────────────────

@dataclass
class OutboxItem:
    """A pending outbox row, as returned by ``claim_pending``."""

    id: int
    event_key: str
    event_type: str
    payload: Dict[str, Any]
    created_at: float
    next_attempt_at: float
    attempt_count: int
    last_error_code: Optional[str]
    last_error_msg: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OutboxItem":
        return cls(
            id=row["id"],
            event_key=row["event_key"],
            event_type=row["event_type"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
            next_attempt_at=row["next_attempt_at"],
            attempt_count=row["attempt_count"],
            last_error_code=row["last_error_code"],
            last_error_msg=row["last_error_msg"],
        )


# ── Operations ───────────────────────────────────────────────────────────────

def enqueue(
    event_type: str,
    payload: Dict[str, Any],
    *,
    event_key: Optional[str] = None,
) -> Tuple[OutboxItem, bool]:
    """Insert an item into the outbox.

    Returns ``(item, created)`` where ``created`` is False when the
    item was already present (matched by ``event_key``). This is the
    contract callers use to detect duplicate submissions.

    Capacity & retention policy (explicit, per the spec):

    * **Contact submissions are ALWAYS accepted.** If the queue is at
      capacity and only contact submissions are pending, the queue
      grows beyond ``SUPABASE_MAX_QUEUE_ITEMS`` rather than rejecting
      the new contact. There is no env var that disables this guarantee.
    * **Analytics events are best-effort.** If the queue is at
      capacity AND a new analytics event arrives, the OLDEST pending
      analytics event is evicted to make room. If no analytics event
      can be evicted (queue is full of contacts), the new analytics
      event is rejected with ``QueueFullError`` and the analytics
      route silently drops it (the user never sees this — analytics
      is fire-and-forget).
    * **Pruning defaults to DISABLED.** The owner prefers long-term
      retention. Set ``SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS`` to a
      positive integer to enable pruning of pending analytics events
      older than that many days. Contact submissions are NEVER pruned
      by this or any other mechanism.
    * **Saturation alerting.** When pending items reach 80% (default)
      of ``SUPABASE_MAX_QUEUE_ITEMS``, the worker logs a WARNING and
      the ``counts()`` snapshot includes a ``saturation_pct`` field
      so the admin dashboard can surface an alert. Tune via
      ``SUPABASE_QUEUE_SATURATION_ALERT_PCT`` (0 disables).
    """
    if event_type not in VALID_TYPES:
        raise ValueError(f"Invalid event_type {event_type!r}")
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")
    if event_key is None:
        event_key = str(uuid.uuid4())

    # Ensure the SQLite path is configured and the schema is initialised
    # BEFORE we try to acquire the module lock. Otherwise we deadlock.
    get_db_path()

    max_items = _env_int("SUPABASE_MAX_QUEUE_ITEMS", DEFAULT_MAX_ITEMS)
    analytics_max_age_days = _env_int(
        "SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS", DEFAULT_ANALYTICS_MAX_AGE_DAYS
    )

    # Pre-flight capacity check. Contacts always pass — analytics degrade
    # first when the queue is near capacity, per the spec.
    if event_type == "analytics_event":
        with _LOCK:
            with _cursor() as cur:
                pending = cur.execute(
                    "SELECT COUNT(*) FROM outbox "
                    "WHERE delivered_at IS NULL AND dead_lettered_at IS NULL"
                ).fetchone()[0]
        if pending >= max_items:
            # Capacity reached. Evict the OLDEST pending analytics event
            # to make room. Contact submissions are NEVER evicted.
            with _LOCK:
                with _cursor() as cur:
                    cur.execute(
                        "DELETE FROM outbox WHERE id IN ("
                        "  SELECT id FROM outbox "
                        "  WHERE event_type = 'analytics_event' "
                        "    AND delivered_at IS NULL "
                        "    AND dead_lettered_at IS NULL "
                        "  ORDER BY next_attempt_at ASC LIMIT 1"
                        ")"
                    )
                    evicted = cur.rowcount
            if evicted == 0:
                # Nothing to evict (queue is full of contacts). Reject
                # the new analytics event — analytics is best-effort.
                raise QueueFullError(
                    "Outbox is full and no analytics event could be evicted "
                    "(queue is at capacity with contact submissions — "
                    "analytics is best-effort and was rejected to preserve "
                    "contacts)."
                )
            log.warning(
                "Queue at capacity (%d pending); evicted 1 oldest analytics "
                "event to make room for new analytics event. Contacts preserved.",
                pending,
            )

    # Prune very old analytics events. Defaults to DISABLED
    # (DEFAULT_ANALYTICS_MAX_AGE_DAYS = 0). The operator may opt in.
    # Contact submissions are NEVER pruned regardless of this setting.
    if event_type == "analytics_event" and analytics_max_age_days > 0:
        cutoff = time.time() - analytics_max_age_days * 86400
        with _LOCK:
            with _cursor() as cur:
                cur.execute(
                    "DELETE FROM outbox WHERE event_type = 'analytics_event' "
                    "AND created_at < ? AND delivered_at IS NULL "
                    "AND dead_lettered_at IS NULL",
                    (cutoff,),
                )

    now = time.time()
    with _LOCK:
        with _cursor() as cur:
            existing = cur.execute(
                "SELECT * FROM outbox WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing is not None:
                return OutboxItem.from_row(existing), False
            cur.execute(
                "INSERT INTO outbox "
                "(event_key, event_type, payload, created_at, next_attempt_at, "
                " attempt_count, last_error_code, last_error_at, last_error_msg, "
                " delivered_at, dead_lettered_at, dead_letter_reason) "
                "VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, NULL)",
                (event_key, event_type, json.dumps(payload, separators=(",", ":")),
                 now, now),
            )
            row = cur.execute(
                "SELECT * FROM outbox WHERE id = last_insert_rowid()"
            ).fetchone()
            return OutboxItem.from_row(row), True


# How long a claimed item is reserved for one worker before another worker
# may take it over. Must exceed the worst-case time to process one batch.
# A crashed worker's lease simply expires, so nothing is stuck in flight.
DEFAULT_LEASE_SECONDS = 300.0


def lease_seconds() -> float:
    try:
        v = float(os.environ.get("SUPABASE_QUEUE_LEASE_SECONDS", DEFAULT_LEASE_SECONDS))
    except ValueError:
        return DEFAULT_LEASE_SECONDS
    return max(30.0, min(v, 3600.0))


_WORKER_ID = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def claim_pending(batch_size: int = 10) -> List[OutboxItem]:
    """Atomically claim up to ``batch_size`` eligible items under a lease.

    Eligible means: not delivered, not dead-lettered, due for an attempt, and
    either unleased or with an expired lease. Selection and update run inside
    ``BEGIN IMMEDIATE`` so that two PROCESSES sharing the file cannot claim the
    same row. Before this change the select and update were separate
    autocommit statements, and four concurrent processes produced about 25%
    duplicate claims (reproduced).

    The caller must call ``mark_delivered``, ``mark_failed`` or
    ``dead_letter`` for each item, which releases the lease.
    """
    if batch_size < 1:
        batch_size = 1
    if batch_size > 100:
        batch_size = 100
    now = time.time()
    get_db_path()
    with _LOCK:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM outbox "
                    "WHERE delivered_at IS NULL AND dead_lettered_at IS NULL "
                    "  AND next_attempt_at <= ? "
                    "  AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                    "ORDER BY next_attempt_at ASC LIMIT ?",
                    (now, now, batch_size),
                ).fetchall()
            ]
            if not ids:
                conn.execute("COMMIT")
                return []
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE outbox SET attempt_count = attempt_count + 1, "
                f"lease_owner = ?, lease_expires_at = ? "
                f"WHERE id IN ({placeholders})",
                (_WORKER_ID, now + lease_seconds(), *ids),
            )
            rows = conn.execute(
                f"SELECT * FROM outbox WHERE id IN ({placeholders}) ORDER BY next_attempt_at ASC",
                tuple(ids),
            ).fetchall()
            conn.execute("COMMIT")
            return [OutboxItem.from_row(r) for r in rows]
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def recover_expired_leases() -> int:
    """Count items whose lease expired (they are claimable again).

    Informational only: expired leases need no repair, because claiming
    already treats them as eligible. Exposed for the admin dashboard so an
    operator can see a worker that died mid-batch.
    """
    get_db_path()
    now = time.time()
    with _LOCK:
        with _cursor() as cur:
            return cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ? AND delivered_at IS NULL "
                "AND dead_lettered_at IS NULL",
                (now,),
            ).fetchone()[0]


def mark_delivered(item_id: int) -> None:
    get_db_path()
    with _LOCK:
        with _cursor() as cur:
            cur.execute(
                "UPDATE outbox SET delivered_at = ?, last_error_code = NULL, "
                "last_error_msg = NULL, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE id = ?",
                (time.time(), item_id),
            )


def mark_failed(
    item_id: int,
    *,
    error_code: str,
    error_msg: str,
    next_attempt_at: float,
) -> None:
    """Record a failed attempt and schedule the next retry."""
    get_db_path()
    with _LOCK:
        with _cursor() as cur:
            cur.execute(
                "UPDATE outbox SET last_error_code = ?, last_error_msg = ?, "
                "last_error_at = ?, next_attempt_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE id = ?",
                (
                    error_code[:120] if error_code else None,
                    (error_msg or "")[:500] or None,
                    time.time(),
                    next_attempt_at,
                    item_id,
                ),
            )


def dead_letter(item_id: int, *, reason: str) -> None:
    """Move an item to the dead-letter state, terminal until manual retry."""
    get_db_path()
    with _LOCK:
        with _cursor() as cur:
            cur.execute(
                "UPDATE outbox SET dead_lettered_at = ?, dead_letter_reason = ?, "
                "lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
                (time.time(), reason[:500], item_id),
            )


def requeue_dead_letter(item_id: int) -> None:
    """Move a dead-lettered item back to the pending pool for retry."""
    get_db_path()
    with _LOCK:
        with _cursor() as cur:
            cur.execute(
                "UPDATE outbox SET dead_lettered_at = NULL, dead_letter_reason = NULL, "
                "next_attempt_at = ?, last_error_code = NULL, last_error_msg = NULL "
                "WHERE id = ?",
                (time.time(), item_id),
            )


def counts() -> Dict[str, Any]:
    """A snapshot of queue counts for the admin UI.

    Includes a ``saturation_pct`` field: the ratio of pending items to
    ``SUPABASE_MAX_QUEUE_ITEMS``, expressed as a percentage. When this
    exceeds ``SUPABASE_QUEUE_SATURATION_ALERT_PCT`` (default 80%),
    the admin dashboard surfaces a saturation alert and the worker
    logs a WARNING every ``SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN``
    minutes (default 15).
    """
    get_db_path()
    max_items = _env_int("SUPABASE_MAX_QUEUE_ITEMS", DEFAULT_MAX_ITEMS)
    with _LOCK:
        with _cursor() as cur:
            pending = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL "
                "AND dead_lettered_at IS NULL"
            ).fetchone()[0]
            in_flight = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL "
                "AND dead_lettered_at IS NULL AND next_attempt_at <= ?",
                (time.time(),),
            ).fetchone()[0]
            retrying = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL "
                "AND dead_lettered_at IS NULL AND next_attempt_at > ?",
                (time.time(),),
            ).fetchone()[0]
            delivered = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL"
            ).fetchone()[0]
            dead_lettered = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE dead_lettered_at IS NOT NULL"
            ).fetchone()[0]
            contact_pending = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type = 'contact_submission' "
                "AND delivered_at IS NULL AND dead_lettered_at IS NULL"
            ).fetchone()[0]
            analytics_pending = cur.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type = 'analytics_event' "
                "AND delivered_at IS NULL AND dead_lettered_at IS NULL"
            ).fetchone()[0]
            oldest_pending_age = cur.execute(
                "SELECT COALESCE(MIN(created_at), 0) FROM outbox "
                "WHERE delivered_at IS NULL AND dead_lettered_at IS NULL"
            ).fetchone()[0]

    saturation_pct = int((pending / max_items) * 100) if max_items > 0 else 0
    alert_pct = _env_int("SUPABASE_QUEUE_SATURATION_ALERT_PCT",
                         DEFAULT_SATURATION_ALERT_PCT)
    return {
        "pending": pending,
        "in_flight": in_flight,
        "retrying": retrying,
        "delivered": delivered,
        "dead_lettered": dead_lettered,
        "contact_pending": contact_pending,
        "analytics_pending": analytics_pending,
        "oldest_pending_age_seconds": max(0.0, time.time() - (oldest_pending_age or time.time())),
        "durable": is_durable(),
        "max_items": max_items,
        "saturation_pct": saturation_pct,
        "saturation_alert_threshold": alert_pct,
        "saturation_alert": bool(alert_pct > 0 and saturation_pct >= alert_pct),
    }


def list_recent(*, limit: int = 100, only_status: Optional[str] = None) -> List[OutboxItem]:
    """Return recent outbox items for the admin UI."""
    if only_status is not None and only_status not in VALID_STATUSES:
        raise ValueError(f"Invalid status {only_status!r}")
    get_db_path()
    with _LOCK:
        with _cursor() as cur:
            if only_status == "delivered":
                q = ("SELECT * FROM outbox WHERE delivered_at IS NOT NULL "
                     "ORDER BY delivered_at DESC LIMIT ?")
            elif only_status == "dead_lettered":
                q = ("SELECT * FROM outbox WHERE dead_lettered_at IS NOT NULL "
                     "ORDER BY dead_lettered_at DESC LIMIT ?")
            elif only_status == "pending":
                q = ("SELECT * FROM outbox WHERE delivered_at IS NULL "
                     "AND dead_lettered_at IS NULL "
                     "ORDER BY next_attempt_at ASC LIMIT ?")
            else:
                q = "SELECT * FROM outbox ORDER BY id DESC LIMIT ?"
            rows = cur.execute(q, (limit,)).fetchall()
            return [OutboxItem.from_row(r) for r in rows]


def list_dead_lettered(*, limit: int = 50) -> List[OutboxItem]:
    return list_recent(limit=limit, only_status="dead_lettered")


# ── Exceptions ───────────────────────────────────────────────────────────────

class QueueFullError(RuntimeError):
    """Raised when the queue is full and cannot accept a new analytics event."""


# ── Helpers ─────────────────────────────────────────────────────────────────

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
