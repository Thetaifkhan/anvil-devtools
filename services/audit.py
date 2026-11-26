"""Admin action auditing.

Every privileged action (login, logout, queue retry, support status change)
records a durable audit trail. Events are enqueued into the local outbox and
delivered to ``public.security_events`` by the background worker, so an
outage never loses an audit record and never blocks the action itself.

Hard rules enforced here:
* The Supabase access token is NEVER recorded — only the verified user id.
* Failures to enqueue are logged and swallowed: an audit hiccup must not
  make the admin action itself fail.
* Payloads are size-capped so a hostile ``details`` dict cannot bloat the
  queue.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from delivery import enqueue
from delivery.outbox import QueueFullError

log = logging.getLogger("anvil.audit")

# Allowed action names. Anything else is a programming error and is rejected
# so the audit trail stays queryable (the DB CHECK also enforces this).
ALLOWED_ACTIONS = (
    "admin_login",
    "admin_login_failed",
    "admin_logout",
    "admin_queue_retry",
    "admin_queue_retry_all",
    "admin_support_update",
)

_MAX_DETAILS_BYTES = 1600
_MAX_ROUTE_CHARS = 120


def record_admin_action(
    action: str,
    *,
    user_id: Optional[str] = None,
    route: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> bool:
    """Queue an audit event. Returns True when it was accepted.

    Best-effort by design: any failure is logged once and swallowed.
    """
    if action not in ALLOWED_ACTIONS:
        log.error("Refusing to audit unknown action %r.", action)
        return False

    import json

    payload: Dict[str, Any] = {
        "action": action,
        "occurred_at": time.time(),
        "user_id": (user_id or "")[:64] or None,
    }
    if route:
        payload["route_name"] = str(route)[:_MAX_ROUTE_CHARS]
    if details:
        trimmed = {
            str(k)[:64]: (v if isinstance(v, (int, float, bool, str)) and len(str(v)) <= 128 else str(v)[:128])
            for k, v in details.items()
        }
        try:
            blob = json.dumps(trimmed, separators=(",", ":"))
        except (TypeError, ValueError):
            blob = "{}"
        if len(blob) > _MAX_DETAILS_BYTES:
            blob = blob[:_MAX_DETAILS_BYTES]
        payload["details"] = blob

    try:
        enqueue("admin_audit", payload)
        return True
    except (QueueFullError, Exception) as exc:  # noqa: BLE001 — never block
        log.error("Could not queue admin audit event %s: %s", action, exc)
        return False
