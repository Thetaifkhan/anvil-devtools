"""Analytics event ingestion endpoint.

POST /api/analytics/event     → record a single analytics event.
POST /api/analytics/session   → upsert a visitor session.

These endpoints are intentionally lenient about failures: they queue the
event into the local durable outbox and return 202 Accepted immediately.
If the payload is invalid (bad event name, missing session_id) they
return 400 — those errors are the browser's fault, not the network's.

The response never blocks on Supabase; the worker delivers asynchronously.
If the queue is full, analytics events are silently dropped (they are
best-effort) but contact submissions are NEVER dropped (see
:mod:`routes.contact`).
"""

from __future__ import annotations

import logging
import time

from flask import Blueprint, jsonify, request

from analytics import (
    hash_ip,
    extract_client_ip,
    trusted_proxy_count_from_env,
    limiter_identity,
    new_event_key,
    sanitize_properties,
    validate_event_payload,
    validate_session_payload,
)
from delivery import enqueue, QueueFullError
from services.errors import ToolError
from services.rate_limit import ANALYTICS_LIMITER

log = logging.getLogger("anvil.analytics.route")

analytics_bp = Blueprint("analytics", __name__, url_prefix="/api/analytics")


def _limiter_key() -> str:
    """Per-client rate-limit key (HMAC of the trusted-proxy client IP).

    Never falls back to a shared bucket: every unauthenticated visitor gets
    their own limit even when IP_HMAC_SECRET is unset.
    """
    return limiter_identity(
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    )


@analytics_bp.post("/event")
def record_event():
    """Queue one analytics event for delivery."""
    allowed, retry_after = ANALYTICS_LIMITER.check(_limiter_key())
    if not allowed:
        return jsonify({
            "ok": False,
            "error": "rate_limited",
            "retry_after_seconds": int(retry_after),
        }), 429

    payload = request.get_json(silent=True) or {}
    event = validate_event_payload(payload)
    if event is None:
        return jsonify({"ok": False, "error": "Invalid event payload."}), 400

    event_key = new_event_key()
    queue_payload = event.to_payload(event_key)
    # Tag with the IP HMAC for abuse analysis on the worker side.
    fwd = request.headers.get("X-Forwarded-For", "")
    remote = request.remote_addr or ""
    ip = extract_client_ip(fwd, remote, trusted_proxy_count_from_env())
    ip_hmac = hash_ip(ip)
    if ip_hmac:
        queue_payload["ip_hmac"] = ip_hmac

    try:
        enqueue("analytics_event", queue_payload, event_key=event_key)
    except QueueFullError:
        # Analytics is best-effort. Don't break the page; the browser
        # already failed silently. Tell the JS to back off for a while.
        return jsonify({"ok": False, "error": "queue_full"}), 503

    return jsonify({"ok": True}), 202


@analytics_bp.post("/session")
def upsert_session():
    """Upsert a visitor session row.

    Sessions are upserted by ``session_id``. The browser generates the
    session_id and visitor_id; we validate them and queue the upsert.
    """
    allowed, retry_after = ANALYTICS_LIMITER.check(_limiter_key())
    if not allowed:
        return jsonify({
            "ok": False,
            "error": "rate_limited",
            "retry_after_seconds": int(retry_after),
        }), 429

    payload = request.get_json(silent=True) or {}
    session = validate_session_payload(payload)
    if session is None:
        return jsonify({"ok": False, "error": "Invalid session payload."}), 400

    # Build a payload that includes the IP HMAC for the visitor_sessions row.
    queue_payload = session.to_payload()
    fwd = request.headers.get("X-Forwarded-For", "")
    remote = request.remote_addr or ""
    ip = extract_client_ip(fwd, remote, trusted_proxy_count_from_env())
    ip_hmac = hash_ip(ip)
    if ip_hmac:
        queue_payload["ip_hmac"] = ip_hmac

    event_key = f"session:{session.session_id}"
    try:
        enqueue("analytics_event", {
            "event_type": "session_upsert",
            "session": queue_payload,
        }, event_key=event_key)
    except QueueFullError:
        return jsonify({"ok": False, "error": "queue_full"}), 503

    return jsonify({"ok": True}), 202
