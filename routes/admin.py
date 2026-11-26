"""Admin dashboard.

All routes under ``/admin/*`` (except ``/admin/login`` POST) require a
verified Flask admin session AND a ``private.is_admin()`` lookup. The
decorator enforces both; do not add a route without it.

Routes:
- GET  /admin/login             → render login form (paste a Supabase token)
- POST /admin/login             → verify token, start session
- POST /admin/logout            → end session
- GET  /admin                   → overview dashboard
- GET  /admin/traffic           → traffic analytics + charts
- GET  /admin/engagement        → session/engagement tables
- GET  /admin/tools             → tool usage + success/error rates
- GET  /admin/support           → support inbox (paginated, filterable)
- GET  /admin/support/<id>      → single submission detail
- POST /admin/support/<id>      → update status, priority, admin_notes
- GET  /admin/audit             → audit log viewer
- GET  /admin/security          → security-event summary
- GET  /admin/queue             → queue status + dead-letter view
- POST /admin/queue/<id>/retry → manually retry a dead-lettered item
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from services.ttl_cache import ttl_cache
from flask import (Blueprint, abort, jsonify, redirect,
                   render_template, request, session, url_for)

import supabase_client
from analytics import limiter_identity
from auth import admin as auth_admin
from delivery import counts as queue_counts, list_dead_lettered, list_recent
from delivery.worker import retry_dead_letter, status_snapshot
from services.audit import record_admin_action
from services.rate_limit import ADMIN_LIMITER

log = logging.getLogger("anvil.admin")

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ── Login / logout ──────────────────────────────────────────────────────────

_cached_ping = ttl_cache(15)(lambda: supabase_client.ping())


@admin_bp.get("/login")
def login():
    if auth_admin.current_admin_user_id():
        return redirect(url_for("admin.overview"))
    return render_template("admin/login.html", error=request.args.get("error"))


@admin_bp.post("/login")
@auth_admin.require_csrf
def login_submit():
    # Per-client throttle. The previous single global key let any anonymous
    # caller lock out the admin login for everyone.
    client = limiter_identity(
        request.headers.get("X-Forwarded-For", ""),
        request.remote_addr or "",
    )
    allowed, retry_after = ADMIN_LIMITER.check(f"admin-login:{client}")
    if not allowed:
        resp = jsonify({
            "ok": False,
            "error": "Too many login attempts. Try again shortly.",
            "retry_after_seconds": int(retry_after),
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(max(1, int(retry_after)))
        return resp

    payload = request.get_json(silent=True) or {}
    unknown = set(payload) - {"access_token"}
    if unknown:
        return jsonify({
            "ok": False,
            "error": "Unknown field(s): " + ", ".join(sorted(unknown)) + ".",
        }), 400
    token = (payload.get("access_token") or "").strip()
    if not token or len(token) > 4096:
        return jsonify({"ok": False, "error": "An access token is required."}), 400

    user = auth_admin.verify_supabase_token(token)
    if user is None:
        record_admin_action("admin_login_failed", route="/admin/login",
                            details={"reason": "verification_failed"})
        return jsonify({
            "ok": False,
            "error": "We could not verify that access token with Supabase.",
        }), 401

    if not auth_admin.is_user_admin(user.user_id):
        log.warning("Verified non-admin token for user_id=%s", user.user_id)
        return jsonify({
            "ok": False,
            "error": "Your Supabase account is not configured as an Anvil admin.",
        }), 403

    # Rotate the session on privilege change: discard anything the browser
    # carried in before authentication (session-fixation hygiene).
    session.clear()
    auth_admin.start_session(user)
    record_admin_action("admin_login", user_id=user.user_id,
                        route="/admin/login")
    return jsonify({"ok": True, "redirect": url_for("admin.overview")})


@admin_bp.post("/logout")
@auth_admin.require_csrf
def logout():
    record_admin_action("admin_logout",
                        user_id=auth_admin.current_admin_user_id(),
                        route="/admin/logout")
    auth_admin.end_session()
    return redirect(url_for("admin.login"))


# ── Overview ───────────────────────────────────────────────────────────────

@admin_bp.get("/")
@auth_admin.require_admin
def overview():
    """The landing dashboard. KPIs from every section."""
    supabase_reachable = _cached_ping() if supabase_client.is_configured() else False
    q = queue_counts()
    db_health = supabase_client.health_state()
    waiting = q.get("contact_pending", 0) + q.get("analytics_pending", 0)
    ctx = {
        "queue": q,
        "waiting": waiting,
        "supabase_configured": supabase_client.is_configured(),
        "supabase_reachable": supabase_reachable,
        "db_health": db_health,
        "kpi": _overview_kpis() if supabase_reachable else None,
        "banner": _overview_banner(supabase_client.is_configured(),
                                   supabase_reachable, q),
    }
    return render_template("admin/overview.html", **ctx)


def _overview_banner(configured: bool, reachable: bool, q: dict):
    """One status banner: green (all good), amber (waiting/retrying),
    red (action needed) plus a single plain sentence saying what to do."""
    if configured and not reachable:
        return {
            "level": "red",
            "title": "Action needed",
            "message": ("The database is not connected. New submissions are "
                        "being kept on this server. Nothing is lost, but "
                        "check the database settings or credentials."),
        }
    if not configured:
        return {
            "level": "amber",
            "title": "Database not set up",
            "message": ("No database is configured, so analytics and contact "
                        "messages are only kept on this server. Set "
                        "DATABASE_URL to start saving them."),
        }
    dead = q.get("dead_lettered", 0)
    if dead:
        return {
            "level": "red",
            "title": "Action needed",
            "message": (f"{dead} item(s) could not be saved after several "
                        "tries. Review them on the Queue page and press "
                        "'Retry all failed' once the problem is fixed."),
        }
    # "Waiting" counts USER-FACING data (contact messages, analytics).
    # Internal admin-audit events are bookkeeping: counting them would make
    # the banner permanently amber right after an admin logs in.
    pending = (q.get("contact_pending", 0) + q.get("analytics_pending", 0))
    retrying = q.get("retrying", 0)
    if pending:
        return {
            "level": "amber",
            "title": "Waiting",
            "message": (f"{pending} item(s) are waiting to be saved"
                        + (f", {retrying} retrying after a temporary problem"
                           if retrying else "")
                        + ". They are sent automatically - no action needed."),
        }
    return {
        "level": "green",
        "title": "All good",
        "message": "Everything is saving normally. Nothing needs your attention.",
    }


@ttl_cache(30)
def _overview_kpis() -> Dict[str, Any]:
    """Pull the headline numbers in a single round-trip."""
    kpi: Dict[str, Any] = {}
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT
                    (SELECT count(*) FROM public.analytics_events
                     WHERE occurred_at > now() - interval '24 hours') AS events_24h,
                    (SELECT count(DISTINCT session_id) FROM public.analytics_events
                     WHERE occurred_at > now() - interval '24 hours'
                       AND session_id IS NOT NULL) AS sessions_24h,
                    (SELECT count(*) FROM public.visitor_sessions
                     WHERE last_seen_at > now() - interval '24 hours') AS active_sessions_24h,
                    (SELECT count(*) FROM public.support_queries
                     WHERE status IN ('new', 'in_progress')) AS open_tickets,
                    (SELECT count(*) FROM public.support_queries
                     WHERE status = 'new') AS new_tickets,
                    (SELECT count(*) FROM public.security_events
                     WHERE occurred_at > now() - interval '24 hours') AS security_24h
            """)
            row = cur.fetchone()
            if row:
                kpi = {
                    "events_24h": row[0] or 0,
                    "sessions_24h": row[1] or 0,
                    "active_sessions_24h": row[2] or 0,
                    "open_tickets": row[3] or 0,
                    "new_tickets": row[4] or 0,
                    "security_24h": row[5] or 0,
                }
    except Exception as exc:
        log.warning("overview KPI query failed: %s", exc)
    return kpi


# ── Traffic ───────────────────────────────────────────────────────────────

@admin_bp.get("/traffic")
@auth_admin.require_admin
def traffic():
    days = _days_param(default=14, max_days=90)
    rows = []
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT
                    date_trunc('day', occurred_at)::date AS day,
                    count(*) FILTER (WHERE event_name = 'page_view') AS page_views,
                    count(DISTINCT session_id) FILTER (WHERE event_name = 'page_view') AS uniques,
                    count(*) FILTER (WHERE event_name = 'tool_run') AS tool_runs,
                    count(*) FILTER (WHERE event_name = 'tool_success') AS tool_success,
                    count(*) FILTER (WHERE event_name = 'tool_error') AS tool_errors
                FROM public.analytics_events
                WHERE occurred_at > now() - (%s || ' days')::interval
                GROUP BY day ORDER BY day ASC
            """, (str(days),))
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("traffic query failed: %s", exc)
        error = str(exc)
    return render_template("admin/traffic.html", days=days,
                           rows=rows, error=error)


# ── Engagement ────────────────────────────────────────────────────────────

@admin_bp.get("/engagement")
@auth_admin.require_admin
def engagement():
    days = _days_param(default=14, max_days=90)
    rows = []
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT
                    date_trunc('day', last_seen_at)::date AS day,
                    count(*) AS sessions,
                    count(*) FILTER (WHERE device_type = 'desktop') AS desktop,
                    count(*) FILTER (WHERE device_type = 'mobile') AS mobile,
                    count(*) FILTER (WHERE device_type = 'tablet') AS tablet,
                    count(*) FILTER (WHERE device_type = 'unknown') AS unknown_device,
                    AVG(engagement_seconds)::int AS avg_engagement_s,
                    MAX(engagement_seconds) AS max_engagement_s
                FROM public.visitor_sessions
                WHERE last_seen_at > now() - (%s || ' days')::interval
                GROUP BY day ORDER BY day ASC
            """, (str(days),))
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("engagement query failed: %s", exc)
        error = str(exc)
    return render_template("admin/engagement.html", days=days,
                           rows=rows, error=error)


# ── Tool usage ────────────────────────────────────────────────────────────

@admin_bp.get("/tools")
@auth_admin.require_admin
def tools():
    days = _days_param(default=30, max_days=180)
    rows = []
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT
                    tool_slug,
                    count(*) FILTER (WHERE event_name = 'tool_run') AS runs,
                    count(*) FILTER (WHERE event_name = 'tool_success') AS successes,
                    count(*) FILTER (WHERE event_name = 'tool_error') AS errors
                FROM public.analytics_events
                WHERE tool_slug IS NOT NULL
                  AND occurred_at > now() - (%s || ' days')::interval
                GROUP BY tool_slug
                ORDER BY runs DESC NULLS LAST
                LIMIT 200
            """, (str(days),))
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("tools query failed: %s", exc)
        error = str(exc)
    return render_template("admin/tools.html", days=days,
                           rows=rows, error=error)


# ── Support inbox ─────────────────────────────────────────────────────────

@admin_bp.get("/support")
@auth_admin.require_admin
def support_inbox():
    status = (request.args.get("status") or "").strip()
    priority = (request.args.get("priority") or "").strip()
    q = (request.args.get("q") or "").strip()
    page = max(1, _int_param("page", default=1))
    per_page = min(100, _int_param("per_page", default=25))
    offset = (page - 1) * per_page

    where_parts = []
    params: List[Any] = []
    if status in ("new", "in_progress", "resolved", "closed"):
        where_parts.append("status = %s")
        params.append(status)
    if priority in ("low", "normal", "high"):
        where_parts.append("priority = %s")
        params.append(priority)
    if q:
        # Reference-code lookup OR loose text search on subject.
        where_parts.append("(reference_code = %s OR subject ILIKE %s)")
        params.extend([q, f"%{q[:200]}%"])
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = []
    total = 0
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM public.support_queries {where_sql}",
                params,
            )
            total = cur.fetchone()[0] or 0
            cur.execute(
                f"""SELECT id, reference_code, name, email, category, subject,
                           status, priority, created_at, updated_at
                    FROM public.support_queries {where_sql}
                    ORDER BY
                        CASE status
                            WHEN 'new' THEN 0
                            WHEN 'in_progress' THEN 1
                            WHEN 'resolved' THEN 2
                            WHEN 'closed' THEN 3
                            ELSE 4
                        END,
                        CASE priority
                            WHEN 'high' THEN 0
                            WHEN 'normal' THEN 1
                            WHEN 'low' THEN 2
                            ELSE 3
                        END,
                        created_at DESC
                    LIMIT %s OFFSET %s""",
                [*params, per_page, offset],
            )
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("support inbox query failed: %s", exc)
        error = str(exc)

    return render_template("admin/support_inbox.html",
                           rows=rows, total=total, page=page, per_page=per_page,
                           status=status, priority=priority, q=q, error=error)


@admin_bp.get("/support/<uuid:submission_id>")
@auth_admin.require_admin
def support_detail(submission_id):
    row = None
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT id, reference_code, name, email, category, subject, message,
                       page_url, status, priority, admin_notes, tool_slug,
                       browser_context, consent_recorded_at,
                       created_at, updated_at, resolved_at
                FROM public.support_queries
                WHERE id = %s
            """, (str(submission_id),))
            row = cur.fetchone()
    except Exception as exc:
        log.warning("support detail query failed: %s", exc)
        error = str(exc)
    if row is None and not error:
        abort(404)
    return render_template("admin/support_detail.html", row=row, error=error)


@admin_bp.post("/support/<uuid:submission_id>")
@auth_admin.require_admin
@auth_admin.require_csrf
def support_update(submission_id):
    payload = request.get_json(silent=True) or {}
    # Reject unknown fields: an admin POST must say exactly what it means.
    allowed_fields = {"status", "priority", "admin_notes"}
    unknown = set(payload) - allowed_fields
    if unknown:
        return jsonify({
            "ok": False,
            "error": "Unknown field(s): " + ", ".join(sorted(unknown)) + ".",
        }), 400
    status = payload.get("status")
    priority = payload.get("priority")
    admin_notes = payload.get("admin_notes")

    sets = []
    params: List[Any] = []
    if status in ("new", "in_progress", "resolved", "closed"):
        sets.append("status = %s")
        params.append(status)
        if status in ("resolved", "closed"):
            sets.append("resolved_at = now()")
        else:
            sets.append("resolved_at = NULL")
    if priority in ("low", "normal", "high"):
        sets.append("priority = %s")
        params.append(priority)
    if isinstance(admin_notes, str):
        if len(admin_notes) > 8000:
            admin_notes = admin_notes[:8000]
        sets.append("admin_notes = %s")
        params.append(admin_notes)

    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update."}), 400

    sets.append("updated_at = now()")
    params.append(str(submission_id))
    try:
        with supabase_client.cursor() as cur:
            cur.execute(
                f"""UPDATE public.support_queries
                    SET {', '.join(sets)}
                    WHERE id = %s""",
                params,
            )
            if cur.rowcount == 0:
                return jsonify({"ok": False, "error": "Submission not found."}), 404
        # The private.capture_audit_change() trigger writes the row-level
        # audit; the outbox-based admin action trail records who did it.
        record_admin_action("admin_support_update",
                            user_id=auth_admin.current_admin_user_id(),
                            route="/admin/support/update",
                            details={"submission_id": str(submission_id),
                                     "status": status,
                                     "priority": priority})
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("support update failed: %s", exc)
        return jsonify({"ok": False, "error": "Update failed."}), 500


# ── Audit viewer ──────────────────────────────────────────────────────────

@admin_bp.get("/audit")
@auth_admin.require_admin
def audit():
    page = max(1, _int_param("page", default=1))
    per_page = min(200, _int_param("per_page", default=50))
    offset = (page - 1) * per_page
    table_filter = (request.args.get("table") or "").strip()
    rows = []
    total = 0
    error = None
    try:
        with supabase_client.cursor() as cur:
            where = ""
            params = []
            if table_filter:
                where = "WHERE table_name = %s"
                params.append(table_filter)
            cur.execute(f"SELECT count(*) FROM private.audit_log {where}", params)
            total = cur.fetchone()[0] or 0
            cur.execute(
                f"""SELECT id, occurred_at, table_schema, table_name, operation,
                           row_pk, changed_by, before_data, after_data
                    FROM private.audit_log {where}
                    ORDER BY occurred_at DESC
                    LIMIT %s OFFSET %s""",
                [*params, per_page, offset],
            )
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("audit query failed: %s", exc)
        error = str(exc)
    return render_template("admin/audit.html", rows=rows, total=total,
                           page=page, per_page=per_page,
                           table_filter=table_filter, error=error)


# ── Security events ───────────────────────────────────────────────────────

@admin_bp.get("/security")
@auth_admin.require_admin
def security():
    days = _days_param(default=14, max_days=90)
    rows = []
    error = None
    try:
        with supabase_client.cursor() as cur:
            cur.execute("""
                SELECT
                    date_trunc('day', occurred_at)::date AS day,
                    event_type,
                    count(*) AS n
                FROM public.security_events
                WHERE occurred_at > now() - (%s || ' days')::interval
                GROUP BY day, event_type
                ORDER BY day DESC, n DESC
            """, (str(days),))
            rows = cur.fetchall()
    except Exception as exc:
        log.warning("security query failed: %s", exc)
        error = str(exc)
    return render_template("admin/security.html", days=days, rows=rows, error=error)


# ── Queue status ──────────────────────────────────────────────────────────

@admin_bp.get("/queue")
@auth_admin.require_admin
def queue_status():
    snap = status_snapshot()
    rows = list_recent(limit=100)
    dead = list_dead_lettered(limit=50)
    return render_template("admin/queue.html", queue=snap, rows=rows,
                           dead_lettered=dead)


@admin_bp.post("/queue/retry-all")
@auth_admin.require_admin
@auth_admin.require_csrf
def queue_retry_all():
    from flask import redirect
    items = list_dead_lettered(limit=1000)
    for item in items:
        retry_dead_letter(item.id)
    record_admin_action("admin_queue_retry_all",
                        user_id=auth_admin.current_admin_user_id(),
                        route="/admin/queue/retry-all",
                        details={"requeued": len(items)})
    return redirect(url_for("admin.overview"))


@admin_bp.post("/queue/<int:item_id>/retry")
@auth_admin.require_admin
@auth_admin.require_csrf
def queue_retry(item_id):
    ok = retry_dead_letter(item_id)
    # Audit every retry attempt, including misses: the audit trail records
    # what the admin did, not only what succeeded.
    record_admin_action("admin_queue_retry",
                        user_id=auth_admin.current_admin_user_id(),
                        route="/admin/queue/retry",
                        details={"item_id": item_id, "found": ok})
    if not ok:
        return jsonify({"ok": False, "error": "Item not dead-lettered."}), 404
    return jsonify({"ok": True})


# ── Health page ────────────────────────────────────────────────────────────

@admin_bp.get("/health")
@auth_admin.require_admin
def health_page():
    """One page answering: is the system healthy, and what is it doing?

    Plain-language operational facts: database state, last successful write,
    last worker run, outbox size and disk space, and the running version.
    """
    import shutil as _shutil
    from delivery.worker import is_worker_running, _is_worker_enabled
    from delivery.worker import _last_run_at, _last_run_items

    db_health = supabase_client.health_state()
    q = queue_counts()
    outbox_path = __import__("delivery.outbox", fromlist=["get_db_path"]).get_db_path()
    try:
        outbox_bytes = os.path.getsize(outbox_path)
    except OSError:
        outbox_bytes = 0
    try:
        usage = _shutil.disk_usage(os.path.dirname(outbox_path) or "/")
        disk_free_mb = usage.free // (1024 * 1024)
        disk_total_mb = usage.total // (1024 * 1024)
    except OSError:
        disk_free_mb = disk_total_mb = None

    return render_template(
        "admin/health.html",
        queue=q,
        db_health=db_health,
        supabase_configured=supabase_client.is_configured(),
        supabase_reachable=(_cached_ping()
                            if supabase_client.is_configured() else False),
        last_success_at=db_health.get("last_success_at") or 0.0,
        worker_running=is_worker_running(),
        worker_enabled=_is_worker_enabled(),
        worker_last_run_at=_last_run_at,
        worker_last_run_items=_last_run_items,
        outbox_path=outbox_path,
        outbox_bytes=outbox_bytes,
        disk_free_mb=disk_free_mb,
        disk_total_mb=disk_total_mb,
        version=os.environ.get("GIT_COMMIT_SHA", "unknown"),
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _days_param(*, default: int, max_days: int) -> int:
    raw = request.args.get("days", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(v, max_days))


def _int_param(name: str, *, default: int) -> int:
    raw = request.args.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
