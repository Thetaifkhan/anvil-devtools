"""Contact page + API.

GET  /contact              → render the polished contact form.
POST /api/contact          → validate, queue, return reference code.

The route handler is intentionally thin — all logic lives in
:mod:`services.contact_service` and :mod:`delivery.outbox`. The form
POSTs to the API endpoint, which returns the reference code on success.

Honesty contract for delivery status
-------------------------------------
The response payload includes a ``delivery_status`` field whose value
reflects the TRUE state of the submission, not what we wish were true:

* ``delivered``      — Supabase confirmed the insert (currently never
                       returned here; we never block the request on DB
                       availability).
* ``queued_durable`` — The submission was durably persisted to the
                       local outbox AND the operator has opted in to
                       durability via ``OUTBOX_DURABLE=1`` (the
                       ``SUPABASE_QUEUE_PATH`` volume must persist
                       across instance replacement).
* ``queued_volatile`` — The submission was accepted into the local
                        outbox but the outbox is NOT on a persistent
                        volume. The user is told the submission is
                        best-effort and given the reference code; an
                        operator must drain the queue before the next
                        deploy to guarantee delivery.
* ``rejected``       — The queue could not accept the submission
                       (queue full and no analytics event could be
                       evicted). The user is told to retry.

The HTTP status code is 202 (Accepted) for both ``queued_durable``
and ``queued_volatile`` — the request was accepted but not yet
delivered. It is 503 for ``rejected``.

The browser-side ``contact.js`` renders different success copy based
on ``delivery_status`` so the user is never misled.
"""

from __future__ import annotations

import logging
import sqlite3

from flask import Blueprint, jsonify, render_template, request

from analytics import limiter_identity
from delivery import enqueue, QueueFullError
from delivery.outbox import is_durable
from services.contact_service import (
    client_idempotency_key,
    validate_submission,
)
from services.errors import ToolError
from services.rate_limit import CONTACT_LIMITER
from tool_registry import TOOLS

log = logging.getLogger("anvil.contact")

contact_bp = Blueprint("contact", __name__)


def _form_post() -> bool:
    """True when a browser submitted the HTML form without JavaScript."""
    ctype = (request.content_type or "").lower()
    return ctype.startswith("application/x-www-form-urlencoded")


def _back(params):
    """Redirect back to the contact page with plain-language params."""
    from urllib.parse import urlencode
    from flask import redirect
    clean = {k: str(v)[:500] for k, v in params.items() if v is not None}
    return redirect("/contact?" + urlencode(clean))


@contact_bp.get("/contact")
def contact_page():
    """Render the contact page. No admin auth required."""
    return render_template("pages/contact.html", tools=TOOLS)


def _limiter_key() -> str:
    """Per-client rate-limit key. Never derived from a client-chosen cookie:
    a cookie is attacker-controlled and would let a bot evade the limit by
    sending a new value on every request."""
    return limiter_identity(
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    )


@contact_bp.post("/api/contact")
def submit_contact():
    """Accept a contact submission, validate it, and queue it."""
    limiter_key = _limiter_key()
    allowed, retry_after = CONTACT_LIMITER.check(limiter_key)
    if not allowed:
        # Log a security event via the queue (best-effort).
        _queue_security_event("rate_limited", limiter_key)
        if _form_post():
            return _back({"error": "Too many submissions. Please try "
                            "again in a few minutes."}), 429
        resp = jsonify({
            "ok": False,
            "error": "Too many submissions. Please try again in a few minutes.",
            "retry_after_seconds": int(retry_after),
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(max(1, int(retry_after)))
        return resp

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        # No-JavaScript fallback: the form posts urlencoded fields.
        payload = request.form.to_dict() if request.form else None
    if not isinstance(payload, dict):
        raise ToolError("Request body must be a JSON object.")

    try:
        submission = validate_submission(payload)
    except ToolError as e:
        if _form_post():
            return _back({"error": e.message}), 400
        return jsonify({"ok": False, "error": e.message}), 400

    # The idempotency event_key: prefer the client-supplied key (so a
    # retried submit doesn't create a duplicate), else the server-generated
    # one in the submission. This guarantees idempotency even across
    # duplicate-submit clicks.
    client_key = client_idempotency_key(payload)
    event_key = client_key or submission.event_key

    queue_payload = submission.to_payload()
    queue_payload["event_key"] = event_key

    try:
        item, created = enqueue(
            "contact_submission",
            queue_payload,
            event_key=event_key,
        )
    except sqlite3.Error as exc:
        # The local queue is broken (corrupt, locked, or the disk
        # is full). Tell the visitor plainly and keep the log for
        # the operator; never show a generic error page.
        log.error("Local outbox failed; contact submission rejected: %s", exc)
        if _form_post():
            return _back({"error": "We cannot save your message right now "
                            "because our local queue is unavailable. Please "
                            "email us directly or try again in a few "
                            "minutes."}), 503
        return jsonify({
            "ok": False,
            "delivery_status": "rejected",
            "error": "We cannot save your message right now because our "
                     "local queue is unavailable. Please email us directly "
                     "or try again in a few minutes.",
            "reference_code": None,
        }), 503
    except QueueFullError:
        # We NEVER silently drop a contact submission. Tell the user
        # to retry and log loudly so an operator can intervene.
        log.error("Outbox full; rejecting contact submission %s.",
                  submission.reference_code)
        if _form_post():
            return _back({"error": "We cannot accept your submission right "
                            "now. Please email us directly or try again "
                            "later."}), 503
        return jsonify({
            "ok": False,
            "delivery_status": "rejected",
            "error": "We cannot accept your submission right now. "
                     "Please email us directly or try again later.",
            "reference_code": None,
        }), 503

    durable = is_durable()
    delivery_status = "queued_durable" if durable else "queued_volatile"

    # When the duplicate-submit detection kicks in (created=False), the
    # original submission is already in the queue with its OWN reference
    # code. We must return THAT code, not the new one we just generated,
    # so the user has a single canonical reference to quote in follow-ups.
    if not created:
        original_ref = item.payload.get("reference_code") if item.payload else None
        return jsonify({
            "ok": True,
            "delivery_status": delivery_status,
            "reference_code": original_ref or submission.reference_code,
            "queued": True,
            "already_queued": True,
            "message": (
                "Your previous submission was already accepted. We did not create "
                "a duplicate. Use the reference code below for any follow-up."
            ),
        }), 202

    if durable:
        message = (
            "Thanks — your submission was accepted and durably queued for "
            "delivery. You can use the reference code to follow up."
        )
    else:
        message = (
            "Thanks — your submission was accepted into the local queue. "
            "Delivery to the database is best-effort: an operator will drain "
            "the queue before any deploy. Use the reference code to follow up."
        )

    if _form_post():
        return _back({"ref": submission.reference_code,
                       "status": delivery_status}), 303
    return jsonify({
        "ok": True,
        "delivery_status": delivery_status,
        "reference_code": submission.reference_code,
        "queued": True,
        "already_queued": False,
        "message": message,
    }), 202


_SECURITY_LOG_INTERVAL_SECONDS = 60.0
_security_log_state = {"last": 0.0, "suppressed": 0}


def _queue_security_event(event_type: str, limiter_key: str) -> None:
    """Record a rate-limit hit WITHOUT writing to the outbox.

    Previously every rate-limited request enqueued an ``admin_audit`` row.
    Those rows are not subject to the analytics capacity check, so a flood
    of 429 responses could grow the queue without bound (confirmed by
    reproduction). Security events are now logged in-process, at most once
    per minute, with a count of suppressed repeats. Nothing is persisted, and
    the limiter key is never logged in full.
    """
    import time as _time
    now = _time.monotonic()
    state = _security_log_state
    if now - state["last"] < _SECURITY_LOG_INTERVAL_SECONDS:
        state["suppressed"] += 1
        return
    log.warning(
        "Rate limit hit on contact (%s); %d similar event(s) suppressed in the last minute. key_prefix=%s",
        event_type, state["suppressed"], limiter_key[:12],
    )
    state["last"] = now
    state["suppressed"] = 0
