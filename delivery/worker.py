"""Background retry worker.

The worker is a single daemon thread that runs inside a gunicorn worker
process when ``SUPABASE_WORKER_ENABLED=1``. It claims pending outbox
items, attempts to deliver them to Supabase, and on failure schedules a
retry with exponential backoff + jitter.

The worker is safe to run in multiple processes against the same outbox
file (SQLite WAL handles locking) and against the same Supabase queue
(the ``private.delivery_queue`` uses ``FOR UPDATE SKIP LOCKED`` for
claiming).

Recommended deployment topologies
---------------------------------
1. **Web service + Background Worker (preferred).** Set
   ``SUPABASE_WORKER_ENABLED=0`` on the web service and run
   ``python -m delivery.worker_cli`` as a separate Render Background
   Worker. The web service NEVER starts a worker thread per request.
2. **Single-instance web service with in-process worker.** Set
   ``SUPABASE_WORKER_ENABLED=1`` AND run gunicorn with ``--workers 1``.
   Using more workers means more threads competing on the same SQLite
   file, which works but is wasteful.

Lifecycle
---------
* ``start_worker()`` launches the thread if it isn't already running.
  Called ONCE at process boot by the app factory when
  ``SUPABASE_WORKER_ENABLED=1``. Do NOT call it from a per-request hook.
* ``stop_worker()`` sets a stop flag; the thread exits at the next poll
  boundary (default 5 s).
* ``run_once()`` processes a single batch and exits — used by the CLI
  for cron-style execution.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import List, Optional

import supabase_client
from delivery.errors import (
    BackoffConfig,
    ErrorClassification,
    classify_exception,
    compute_backoff,
)
from delivery.outbox import (
    OutboxItem,
    counts as queue_counts,
    claim_pending,
    configure as configure_outbox,
    dead_letter,
    list_dead_lettered,
    mark_delivered,
    mark_failed,
    requeue_dead_letter,
)

log = logging.getLogger("anvil.delivery.worker")

# Iteration-level failures (an exception escaping run_once, e.g. the local
# SQLite file is briefly locked) back the loop off exponentially, capped
# at this many seconds. The worker NEVER exits on its own: the previous
# behaviour stopped delivery permanently after 50 consecutive failures,
# which contradicts the requirement that retries survive outages.
_ITERATION_BACKOFF_CAP_SECONDS = 300.0

# Module-level lock around start/stop so two threads can't race the
# thread creation in unexpected ways.
_LIFECYCLE_LOCK = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_worker_pid: Optional[int] = None
_worker_stop = threading.Event()
_consecutive_iteration_failures = 0

# Health-page instrumentation: when did the worker last run, and how many
# items did that run attempt? Updated by run_once(); read by /admin/health.
_last_run_at = 0.0
_last_run_items = 0

# Track the last time we logged a saturation alert so we don't spam logs.
# Saturation is checked once per poll cycle (every SUPABASE_WORKER_POLL_SECONDS
# seconds, default 5). With the cooldown at 15 minutes, that's at most one
# WARNING log per 180 polls.
_last_saturation_alert_at = 0.0


# ── Lifecycle ────────────────────────────────────────────────────────────────

def start_worker() -> None:
    """Start the background worker thread for THIS process if not running.

    Idempotent per process. A thread does not survive ``fork()``, so the
    recorded PID is compared with the current PID. Without that check, a
    worker started before a fork (for example by gunicorn ``--preload``)
    would look alive in the child and never run. See ``_after_fork_in_child``.
    """
    global _worker_thread, _worker_pid
    if not _is_worker_enabled():
        log.debug("Queue worker disabled (SUPABASE_WORKER_ENABLED!=1).")
        return
    with _LIFECYCLE_LOCK:
        if (
            _worker_thread is not None
            and _worker_thread.is_alive()
            and _worker_pid == os.getpid()
        ):
            return
        _worker_stop.clear()
        configure_outbox()
        t = threading.Thread(target=_worker_loop, name="anvil-delivery-worker", daemon=True)
        _worker_thread = t
        _worker_pid = os.getpid()
        t.start()
        log.info("Queue worker thread started (pid %d).", _worker_pid)


def _after_fork_in_child() -> None:
    """Reset process-local state in a freshly forked child.

    Three things are inherited from the parent and must not be reused:

    1. Locks. The parent's worker thread may hold a module lock at the instant
       of fork(). The child inherits that lock in the locked state and nothing
       in the child will ever release it. Reproduced: the child blocked forever
       inside configure_outbox(). Fresh locks are created here.
    2. The database connection. Sharing a socket between two processes
       interleaves their protocol streams and corrupts both. The engine is
       discarded so the child opens its own connection.
    3. The worker thread object. Threads do not survive fork(), so the stale
       handle is cleared before a new worker is started for this process.
    """
    global _worker_thread, _worker_pid, _LIFECYCLE_LOCK, _worker_stop
    from delivery import outbox as _outbox
    import supabase_client as _supabase
    _outbox._LOCK = threading.Lock()
    _supabase._LOCK = threading.Lock()
    _supabase._ENGINE = None
    _supabase.reset_health()
    _LIFECYCLE_LOCK = threading.Lock()
    _worker_stop = threading.Event()
    _worker_thread = None
    _worker_pid = None
    if _is_worker_enabled():
        start_worker()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def stop_worker(timeout: float = 5.0) -> None:
    """Stop the worker thread. Used by tests and graceful shutdown."""
    global _worker_thread
    _worker_stop.set()
    with _LIFECYCLE_LOCK:
        t = _worker_thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        _worker_thread = None


def is_worker_running() -> bool:
    """True if the worker thread is alive IN THIS PROCESS. Used by tests and /admin/queue."""
    return (
        _worker_thread is not None
        and _worker_thread.is_alive()
        and _worker_pid == os.getpid()
    )


def _is_worker_enabled() -> bool:
    raw = os.environ.get("SUPABASE_WORKER_ENABLED", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _poll_seconds() -> float:
    try:
        return max(1.0, float(os.environ.get("SUPABASE_WORKER_POLL_SECONDS", "5")))
    except ValueError:
        return 5.0


# ── Main loop ────────────────────────────────────────────────────────────────

def _worker_loop() -> None:
    """The daemon thread's main loop. Never raises and never exits on its own."""
    global _last_saturation_alert_at, _consecutive_iteration_failures
    poll = _poll_seconds()
    consecutive_failures = 0
    while not _worker_stop.is_set():
        try:
            processed = run_once()
            consecutive_failures = 0
            _consecutive_iteration_failures = 0
            # Check queue saturation — log a WARNING if the queue is
            # near capacity and we haven't alerted recently. Contact
            # submissions are always preserved; the warning surfaces
            # the situation so an operator can intervene before
            # analytics events start being evicted.
            try:
                from delivery.outbox import counts as _counts
                snap = _counts()
                if snap.get("saturation_alert"):
                    import time as _time
                    cooldown = _saturation_cooldown_seconds()
                    now = _time.time()
                    if now - _last_saturation_alert_at >= cooldown:
                        log.warning(
                            "Queue saturation alert: %d/%d pending items (%d%%). "
                            "Contact submissions: %d (always preserved). "
                            "Analytics events: %d (will be evicted if queue fills). "
                            "Investigate delivery failures or scale the worker.",
                            snap["pending"], snap["max_items"],
                            snap["saturation_pct"],
                            snap["contact_pending"], snap["analytics_pending"],
                        )
                        _last_saturation_alert_at = now
            except Exception:  # pragma: no cover — defensive
                pass
            if processed == 0:
                # Nothing to do — sleep the full poll interval.
                _worker_stop.wait(poll)
            else:
                # Processed a batch; loop again with a short sleep so we
                # don't pin a CPU on a flood of items.
                _worker_stop.wait(min(poll, 0.5))
        except Exception as exc:  # pragma: no cover — defensive
            # Never exit the loop. Back off (capped) and keep trying: a
            # stopped worker silently stops all delivery.
            consecutive_failures += 1
            _consecutive_iteration_failures = consecutive_failures
            if consecutive_failures == 1 or consecutive_failures % 50 == 0:
                log.exception(
                    "Worker iteration failed (%d consecutive); retrying with backoff: %s",
                    consecutive_failures, exc,
                )
            delay = min(poll * (2 ** min(consecutive_failures, 8)), _ITERATION_BACKOFF_CAP_SECONDS)
            _worker_stop.wait(delay)


def _saturation_cooldown_seconds() -> float:
    """Return the saturation-alert cooldown in seconds.

    Reads ``SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN`` (default 15
    minutes). Returns at least 60 seconds to prevent log spam.
    """
    try:
        return max(60.0, float(os.environ.get(
            "SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN",
            str(15)
        )) * 60.0)
    except (TypeError, ValueError):
        return 900.0


def run_once(batch_size: int = 10) -> int:
    """Process a single batch of pending items. Returns count attempted."""
    global _last_run_at, _last_run_items
    _last_run_at = time.time()
    items = claim_pending(batch_size=batch_size)
    _last_run_items = len(items)
    if not items:
        return 0
    log.info("Worker claimed %d outbox item(s).", len(items))

    if not supabase_client.is_configured():
        # No Supabase configured — requeue with a long backoff so we don't
        # spin on a misconfigured deployment. Items stay in the outbox.
        cfg = BackoffConfig.from_env()
        backoff = compute_backoff(0, cfg)
        future = time.time() + backoff
        for item in items:
            mark_failed(
                item.id,
                error_code="no_supabase_configured",
                error_msg="Supabase DATABASE_URL is not set; items will remain queued.",
                next_attempt_at=future,
            )
            log.warning(
                "Requeued item %s (%s): no Supabase configuration.",
                item.event_key, item.event_type,
            )
        return len(items)

    for item in items:
        _process_item(item)
    return len(items)


# ── Per-item processing ──────────────────────────────────────────────────────

def _process_item(item: OutboxItem) -> None:
    """Attempt to deliver one item. Idempotent on the Supabase side."""
    handler = _HANDLERS.get(item.event_type)
    if handler is None:
        dead_letter(item.id, reason=f"unknown_event_type:{item.event_type}")
        log.error("Dead-lettered item %s: unknown event type %s",
                  item.event_key, item.event_type)
        return

    try:
        handler(item)
        mark_delivered(item.id)
        log.info("Delivered item %s (%s) after %d attempt(s).",
                 item.event_key, item.event_type, item.attempt_count)
    except Exception as exc:
        cls = classify_exception(exc)
        _handle_failure(item, exc, cls)


# Short, plain-language dead-letter reasons. The SQLSTATE/code stays in
# last_error_code and the full exception text in last_error_msg.
_DEAD_LETTER_REASONS = {
    "22P02": "Invalid data format",
    "22P05": "Untranslatable characters",
    "22001": "Text too long for the database",
    "23502": "A required field was missing",
    "23503": "Related record not found",
    "23505": "Duplicate record",
    "23514": "Data rejected by a database rule",
    "23P01": "Conflicting record",
    "42P01": "Database table missing (migration not run?)",
    "42703": "Database column missing (migration not run?)",
    "42804": "Data type mismatch",
    "42P10": "Invalid column reference",
    "42P17": "Invalid database object",
    "42501": "Permission denied by the database",
    "unknown_event_type": "Unknown event type",
}


def _readable_dead_letter_reason(cls: ErrorClassification) -> str:
    return _DEAD_LETTER_REASONS.get(cls.code, "Unexpected error")[:200]


def _handle_failure(item: OutboxItem, exc: Exception, cls: ErrorClassification) -> None:
    """Schedule the next retry or dead-letter the item."""
    log.warning(
        "Item %s (%s) failed (attempt %d, code=%s, transient=%s): %s",
        item.event_key, item.event_type, item.attempt_count,
        cls.code, cls.transient, exc,
    )

    if not cls.transient:
        # Permanent failure — the payload or schema is wrong, retrying won't
        # help. Give the operator a SHORT readable reason in the dashboard;
        # keep the full error separately for developers.
        reason = _readable_dead_letter_reason(cls)
        # Record the full error on the row first (last_error_* columns are
        # the developer-facing detail), then the terminal state.
        mark_failed(
            item.id,
            error_code=cls.code,
            error_msg=str(exc)[:500],
            next_attempt_at=time.time(),
        )
        dead_letter(item.id, reason=reason)
        log.error("Dead-lettered item %s: %s (code=%s)",
                  item.event_key, reason, cls.code)
        return

    # Transient: schedule a retry with exponential backoff + jitter.
    backoff = compute_backoff(item.attempt_count)
    next_attempt_at = time.time() + backoff
    mark_failed(
        item.id,
        error_code=cls.code,
        error_msg=str(exc)[:500],
        next_attempt_at=next_attempt_at,
    )
    log.info("Requeued item %s for retry in %.1fs (attempt %d → %d).",
             item.event_key, backoff, item.attempt_count, item.attempt_count + 1)


# ── Timestamp schema map ────────────────────────────────────────────────────
# Which payload fields are timestamps (epoch seconds) that must become
# timezone-aware datetimes before they reach Postgres. The worker converts
# EXACTLY these fields; anything not listed is passed through untouched.
#
# REGRESSION GUARD: tests/test_phase2_outbox.py asserts that every
# ``*_at`` field known for each event type appears here. Adding a new
# timestamp-ish field without registering it will fail that test.
TIMESTAMP_FIELDS = {
    "analytics_event": {"occurred_at"},
    "analytics_event_session": {"started_at", "last_seen_at", "ended_at"},
    "contact_submission": {"consent_recorded_at", "occurred_at", "created_at"},
    "admin_audit": {"occurred_at"},
}

# All fields each event type may carry (for the guard test + clamping).
KNOWN_FIELDS = {
    "analytics_event": {
        "event_name", "session_id", "tool_slug", "page_path", "properties",
        "occurred_at", "ip_hmac",
    },
    "analytics_event_session": {
        "session_id", "visitor_id", "started_at", "last_seen_at", "ended_at",
        "engagement_seconds", "landing_page", "referrer_domain",
        "device_type", "country_code", "ip_hmac",
    },
    "contact_submission": {
        "reference_code", "name", "email", "category", "subject", "message",
        "page_url", "tool_slug", "browser_context", "consent_recorded_at",
        "event_key", "occurred_at", "created_at",
    },
    "admin_audit": {
        "action", "user_id", "occurred_at", "route_name", "details",
    },
}

# Clock-skew clamping window (see §2.6): event times more than this far in
# the future, or older than this, are clamped and logged, never stored raw.
MAX_FUTURE_SKEW_SECONDS = 5 * 60          # 5 minutes ahead
MAX_AGE_SECONDS = 365 * 24 * 3600         # one year back


def _clamp_epoch(value):
    """Clamp an epoch number into a sane window; pass others through."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    import time as _time
    now = _time.time()
    if value > now + MAX_FUTURE_SKEW_SECONDS:
        log.warning(
            "Event timestamp %.0f is in the future (clock skew?); clamping "
            "to now.", value,
        )
        return now
    if value < now - MAX_AGE_SECONDS:
        log.warning(
            "Event timestamp %.0f is older than %d days; clamping to now.",
            value, MAX_AGE_SECONDS // 86400,
        )
        return now
    return value


def _convert_timestamps(event_type: str, payload: dict) -> dict:
    """Return a payload copy whose registered timestamp fields are clamped.

    The actual epoch -> datetime conversion stays in ``_to_ts`` at SQL
    binding time; this pass enforces the schema map and the clamping so a
    skewed client clock can never store year-20380 or 1970 dates.
    """
    fields = TIMESTAMP_FIELDS.get(event_type, set())
    if not fields:
        return payload
    out = dict(payload)
    for f in fields:
        if f in out:
            out[f] = _clamp_epoch(out[f])
    return out


# ── Event handlers ────────────────────────────────────────────────────────────

def _handle_analytics_event(item: OutboxItem) -> None:
    """Insert an analytics event or upsert a session into Supabase.

    The outbox ``event_key`` is used as a unique idempotency key. We first
    insert into ``private.delivery_queue`` (the Postgres-side claim pool),
    then upsert into the appropriate public table.

    Two payload shapes are supported:

    1. **Plain event** — has ``event_name`` and ``session_id``:
       inserted/upserted into ``public.analytics_events``.
    2. **Session upsert** — has a ``session`` key with the session fields:
       upserted into ``public.visitor_sessions`` by ``session_id``.

    Both shapes are idempotent: re-processing a row with the same
    event_key never produces duplicate rows in the database, even if
    the previous attempt committed on the server but the response was
    lost (ambiguous commit).
    """
    payload = _convert_timestamps("analytics_event", item.payload)
    _delivery_queue_upsert(item.event_key, "analytics_event", payload)

    if "session" in payload and "event_name" not in payload:
        _upsert_session(item.event_key,
                        _convert_timestamps("analytics_event_session",
                                            payload["session"]))
        return

    # Plain analytics event. The schema has no UNIQUE constraint on
    # analytics_events.id, so we use a sentinel: insert with ON CONFLICT
    # DO NOTHING against the delivery_queue's event_key, and rely on
    # the unique event_key at the outbox + delivery_queue layer to
    # prevent duplicates.
    sql = """
        INSERT INTO public.analytics_events
            (occurred_at, event_name, session_id, tool_slug, page_path, properties)
        VALUES (%s, %s,
                (SELECT v.id FROM public.visitor_sessions v WHERE v.id = %s::uuid),
                %s, %s, %s)
        ON CONFLICT DO NOTHING
    """
    params = (
        _to_ts(payload.get("occurred_at")),
        payload.get("event_name"),
        payload.get("session_id"),
        payload.get("tool_slug"),
        payload.get("page_path"),
        _to_jsonb(payload.get("properties") or {}),
    )
    with supabase_client.cursor() as cur:
        cur.execute(sql, params)


def _upsert_session(event_key: str, payload: dict) -> None:
    """Upsert a visitor_sessions row, idempotently by session_id.

    Engagement time accumulates across session upserts: we take the
    MAX(engagement_seconds) seen so far rather than overwriting, so a
    paused-tab heart-beat doesn't reset the timer.
    """
    sql = """
        INSERT INTO public.visitor_sessions
            (id, visitor_id, started_at, last_seen_at, ended_at,
             engagement_seconds, landing_page, referrer_domain, device_type,
             country_code, ip_hmac)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            last_seen_at = GREATEST(visitor_sessions.last_seen_at, EXCLUDED.last_seen_at),
            ended_at = EXCLUDED.ended_at,
            engagement_seconds = GREATEST(
                visitor_sessions.engagement_seconds,
                EXCLUDED.engagement_seconds
            ),
            ip_hmac = COALESCE(EXCLUDED.ip_hmac, visitor_sessions.ip_hmac)
    """
    params = (
        payload.get("session_id"),
        payload.get("visitor_id"),
        _to_ts(payload.get("started_at")),
        _to_ts(payload.get("last_seen_at")),
        _to_ts(payload.get("ended_at")),
        payload.get("engagement_seconds"),
        payload.get("landing_page"),
        payload.get("referrer_domain"),
        payload.get("device_type"),
        payload.get("country_code"),
        payload.get("ip_hmac"),
    )
    with supabase_client.cursor() as cur:
        cur.execute(sql, params)


def _handle_contact_submission(item: OutboxItem) -> None:
    """Insert a contact submission into Supabase, idempotently.

    Idempotency is enforced at three layers:

    1. Outbox-level: ``outbox.event_key`` is UNIQUE. A duplicate insert
       with the same key returns the existing row — the worker never
       even reaches this handler for a duplicate.
    2. ``private.delivery_queue``: ``event_key UUID NOT NULL UNIQUE``
       with ``ON CONFLICT (event_key) DO NOTHING``. Survives worker
       crashes between the public-table INSERT and the delivery-queue
       INSERT.
    3. ``public.support_queries.reference_code``: a partial unique
       index added in migration 004. ``ON CONFLICT (reference_code)
       DO NOTHING``. Survives an ambiguous commit (server committed
       but the client never saw the response).

    Layer 3 is the critical one for the ambiguous-commit scenario:
    even if the worker's previous attempt successfully inserted the
    row but the connection dropped before the response arrived, the
    retry's INSERT will conflict on reference_code and do nothing.
    """
    payload = _convert_timestamps("contact_submission", item.payload)
    _delivery_queue_upsert(item.event_key, "contact_submission", payload)

    sql = """
        INSERT INTO public.support_queries
            (reference_code, name, email, category, subject, message,
             page_url, status, priority, tool_slug, browser_context,
             consent_recorded_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', 'normal', %s, %s, %s, now(), now())
        ON CONFLICT (reference_code) DO NOTHING
    """
    params = (
        payload.get("reference_code"),
        payload.get("name"),
        payload.get("email"),
        payload.get("category"),
        payload.get("subject"),
        payload.get("message"),
        payload.get("page_url"),
        payload.get("tool_slug"),
        _to_jsonb(payload.get("browser_context") or {}),
        payload.get("consent_recorded_at"),
    )
    with supabase_client.cursor() as cur:
        cur.execute(sql, params)


def _handle_admin_audit(item: OutboxItem) -> None:
    """Insert an admin action into public.security_events.

    Queued by services/audit.py for login, logout, queue retry and
    support status changes. The payload NEVER contains the token -
    only the verified user id, the action name and a timestamp.
    Idempotent via the outbox/delivery_queue event_key layers.
    """
    import json as _json
    payload = _convert_timestamps("admin_audit", item.payload)
    action = payload.get("action") or "unknown"
    details = payload.get("details")
    if isinstance(details, str):
        try:
            details = _json.loads(details)
        except ValueError:
            details = {"raw": details[:256]}
    if not isinstance(details, dict):
        details = {}
    details = dict(details)
    details["admin_action"] = action
    details["user_id"] = payload.get("user_id")
    with supabase_client.cursor() as cur:
        cur.execute(
            """INSERT INTO public.security_events
                   (occurred_at, event_type, route_name, details)
               VALUES (%s, 'admin_action', %s, %s)""",
            (_to_ts(payload.get("occurred_at")),
             (payload.get("route_name") or None),
             _to_jsonb(details)),
        )


_HANDLERS = {
    "analytics_event": _handle_analytics_event,
    "contact_submission": _handle_contact_submission,
    "admin_audit": _handle_admin_audit,
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _delivery_queue_upsert(event_key: str, event_type: str, payload: dict) -> None:
    """Insert into private.delivery_queue if not already present.

    This is the SECOND idempotency layer (after the outbox). It catches
    the ambiguous-commit case: if the worker's previous attempt
    successfully committed to public.support_queries but died before
    marking itself delivered, the retry will find the delivery_queue
    row already present (via ON CONFLICT) and know the public-table
    insert was already done.

    Without this layer, a retry could still be safe thanks to the
    public-table ON CONFLICT clauses, but the delivery_queue gives
    us an explicit "was this attempt's payload ever delivered?" record
    that survives across worker restarts.
    """
    try:
        with supabase_client.cursor() as cur:
            cur.execute(
                """
                INSERT INTO private.delivery_queue
                    (event_key, event_type, payload)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_key) DO NOTHING
                """,
                (event_key, event_type, _to_jsonb(payload)),
            )
    except Exception as exc:
        # If private.delivery_queue doesn't exist yet (migration not run),
        # log and continue — the public-table ON CONFLICT clauses still
        # provide idempotency for the actual delivery.
        cls = classify_exception(exc)
        if cls.code in {"42P01", "42P02"}:  # undefined_table / undefined_column
            log.debug(
                "private.delivery_queue not available (%s); "
                "falling back to public-table-only idempotency.",
                cls.code,
            )
        else:
            raise


def _to_ts(value):
    """Return a timezone-aware datetime for a timestamptz column.

    The app stores event times as epoch seconds (float). Postgres rejects a
    float for a timestamp column, so convert here. Datetimes and strings pass
    through unchanged.
    """
    from datetime import datetime, timezone
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    return value


def _to_jsonb(value) -> str:
    """Convert a Python dict to a JSON string for the JSONB parameter."""
    import json
    if isinstance(value, str):
        # Already a JSON string — pass through.
        return value
    return json.dumps(value or {}, separators=(",", ":"))


# ── Admin CLI helpers (used by /admin/queue "retry dead-letter" buttons) ────

def retry_dead_letter(item_id: int) -> bool:
    """Move a dead-lettered item back to the pending pool. Returns True on
    success, False if the item was not in the dead-letter state."""
    items = list_dead_lettered(limit=1000)
    if not any(i.id == item_id for i in items):
        return False
    requeue_dead_letter(item_id)
    log.info("Requeued dead-lettered item %d for retry.", item_id)
    return True


def status_snapshot() -> dict:
    """Snapshot of queue + DB connectivity, used by /admin/queue."""
    q = queue_counts()
    q["supabase_configured"] = supabase_client.is_configured()
    q["supabase_reachable"] = supabase_client.ping() if q["supabase_configured"] else False
    q["db_health"] = supabase_client.health_state()
    q["worker_running"] = is_worker_running()
    q["worker_enabled"] = _is_worker_enabled()
    q["worker_consecutive_failures"] = _consecutive_iteration_failures
    q["worker_last_run_at"] = _last_run_at
    q["worker_last_run_items"] = _last_run_items
    return q
