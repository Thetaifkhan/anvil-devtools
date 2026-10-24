"""Admin authentication.

The project's authorization model lives in the database:
``private.is_admin()`` checks ``auth.uid()`` against ``public.admin_profiles``.
Flask doesn't run inside Postgres, so we cannot call ``private.is_admin()``
directly — but we CAN verify a Supabase-issued JWT server-side and then
ask the database whether the user is an admin.

Flow
----
1. The operator visits ``/admin/login`` and pastes a Supabase access
   token they obtained from the Supabase dashboard's SQL editor or auth
   session. (For production multi-admin setups you'd build a proper
   OAuth flow; see "Production hardening" below.)
2. The login route verifies the JWT with the Supabase auth API:
   ``GET <SUPABASE_URL>/auth/v1/user`` with ``Authorization: Bearer <jwt>``.
   A 200 response means the token is valid and unexpired.
3. The route then issues a Flask session cookie carrying the verified
   ``user_id`` (a UUID) and a server-side TTL.
4. On every subsequent ``/admin/*`` request, ``require_admin`` re-checks
   the session and asks Postgres ``SELECT private.is_admin()`` to
   authorize the action. JWTs are never trusted on their own.

Security guarantees
-------------------
- Service-role key never leaves the server.
- Browser never receives the service-role key.
- JWTs are validated against Supabase, not decoded blindly.
- Admin status is checked on every request via ``private.is_admin()``.
- Sessions expire after a short TTL (default 4 hours).
- The session cookie is signed by Flask's ``SECRET_KEY`` (HMAC).

Production hardening (TODO for the operator)
---------------------------------------------
- Replace the paste-token login with a real Supabase OAuth redirect
  (``/auth/v1/authorize?provider=github`` etc.) so admins never copy
  tokens by hand.
- Set ``SESSION_COOKIE_SECURE=True`` and ``SESSION_COOKIE_HTTPONLY=True``
  (already on by default in Flask).
- Consider CSRF protection on state-changing admin endpoints — see the
  ``CSRFToken`` middleware in :mod:`app`.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from functools import wraps
from typing import Optional

import requests
from flask import current_app, redirect, request, session, url_for

log = logging.getLogger("anvil.auth")

SESSION_KEY_USER_ID = "anvil_admin_uid"
SESSION_KEY_USER_EMAIL = "anvil_admin_email"
SESSION_KEY_ISSUED_AT = "anvil_admin_iat"
SESSION_TTL_SECONDS = 4 * 3600  # 4 hours

# CSRF: a per-session random token. State-changing admin requests must echo
# it in the X-CSRF-Token header (JSON) or a csrf_token form field. A custom
# header forces a CORS preflight, which a cross-site page cannot pass, and the
# token additionally proves the request came from a page this session rendered.
CSRF_SESSION_KEY = "anvil_csrf"
CSRF_HEADER = "X-CSRF-Token"

# Cache the admin lookup result for a short window to avoid hammering
# Postgres on every request. 60 s is enough that a revoke propagates
# within a minute but not so short that we add measurable latency.
_ADMIN_CACHE_TTL = 60.0
_admin_cache: dict[str, tuple[float, bool]] = {}


@dataclass(frozen=True)
class VerifiedUser:
    user_id: str
    email: Optional[str]


def verify_supabase_token(access_token: str) -> Optional[VerifiedUser]:
    """Verify a Supabase access token by calling the auth API.

    Returns None on any failure (invalid token, expired, network error,
    missing config). Never raises — the caller turns the None into a
    401 response.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "").strip()
    if not supabase_url or not anon_key:
        log.warning("Supabase auth verification skipped: SUPABASE_URL or anon key missing.")
        return None

    if not access_token or len(access_token) > 4096:
        return None

    try:
        resp = requests.get(
            f"{supabase_url}/auth/v1/user",
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {access_token}",
            },
            timeout=5.0,
        )
    except requests.RequestException as exc:
        log.warning("Supabase auth verify failed (transport): %s", exc)
        return None

    if resp.status_code != 200:
        log.info("Supabase auth verify failed: HTTP %s", resp.status_code)
        return None

    try:
        body = resp.json()
    except ValueError:
        return None

    user_id = body.get("id")
    if not user_id:
        return None
    email = body.get("email")
    return VerifiedUser(user_id=str(user_id), email=email)


def start_session(user: VerifiedUser) -> None:
    """Begin a Flask admin session for the verified user."""
    session[SESSION_KEY_USER_ID] = user.user_id
    session[SESSION_KEY_USER_EMAIL] = user.email
    session[SESSION_KEY_ISSUED_AT] = time.time()
    session.permanent = True


def end_session() -> None:
    """End the admin session."""
    for key in (SESSION_KEY_USER_ID, SESSION_KEY_USER_EMAIL, SESSION_KEY_ISSUED_AT):
        session.pop(key, None)


def current_admin_user_id() -> Optional[str]:
    """Return the verified admin's user_id from the session, or None."""
    uid = session.get(SESSION_KEY_USER_ID)
    if not uid:
        return None
    issued_at = session.get(SESSION_KEY_ISSUED_AT) or 0
    if time.time() - issued_at > SESSION_TTL_SECONDS:
        end_session()
        return None
    return str(uid)


def is_user_admin(user_id: str) -> bool:
    """Ask Postgres whether the given user_id is an admin.

    Uses the existing ``private.is_admin()`` SQL function which checks
    ``auth.uid()`` against ``public.admin_profiles``. Because the service
    role bypasses RLS, we set the local ``request.jwt.claim.sub`` so
    ``auth.uid()`` returns our verified user_id inside the SQL.
    """
    if not user_id:
        return False
    cached = _admin_cache.get(user_id)
    if cached is not None and time.time() - cached[0] < _ADMIN_CACHE_TTL:
        return cached[1]

    import supabase_client
    if not supabase_client.is_configured():
        return False
    try:
        with supabase_client.cursor() as cur:
            # set_config with is_local=true lives only for this transaction
            cur.execute(
                "SELECT set_config('request.jwt.claim.sub', %s, true)",
                (user_id,),
            )
            cur.execute("SELECT private.is_admin()")
            row = cur.fetchone()
            result = bool(row and row[0])
    except Exception as exc:
        log.warning("is_admin lookup failed for %s: %s", user_id, exc)
        result = False

    _admin_cache[user_id] = (time.time(), result)
    return result


def require_admin(view_func):
    """Decorator that requires the caller to be a logged-in admin.

    On failure it redirects to the admin login page (for HTML) or
    returns 401 JSON (for /api/admin/*). The session and the database
    are both consulted; the session alone is never sufficient.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        uid = current_admin_user_id()
        if not uid:
            if _wants_json():
                return _unauthorized_json()
            return redirect(url_for("admin.login"))
        if not is_user_admin(uid):
            end_session()
            log.warning("Denied admin access for user_id=%s (not an admin).", uid)
            if _wants_json():
                return _forbidden_json()
            return redirect(url_for("admin.login", error="not_admin"))
        return view_func(*args, **kwargs)
    return wrapper


# ── Helpers ─────────────────────────────────────────────────────────────────

def csrf_token() -> str:
    """Return this session's CSRF token, creating it on first use.

    Called only from admin templates, so anonymous public visitors never get a
    session cookie from it.
    """
    tok = session.get(CSRF_SESSION_KEY)
    if not tok:
        tok = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = tok
    return tok


def require_csrf(view_func):
    """Reject a state-changing request unless it carries this session's token.

    Apply INSIDE ``require_admin`` so unauthenticated callers still get 401
    first. Comparison is constant-time.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        expected = session.get(CSRF_SESSION_KEY)
        supplied = request.headers.get(CSRF_HEADER) or request.form.get("csrf_token") or ""
        if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
            log.warning("CSRF check failed for %s %s.", request.method, request.path)
            if _wants_json():
                return _csrf_failed_json()
            return ("Your session token is missing or expired. Reload the page and try again.", 403)
        return view_func(*args, **kwargs)
    return wrapper


def _csrf_failed_json():
    from flask import jsonify
    return jsonify({"ok": False, "error": "Security token missing or invalid. Reload and retry."}), 403


def _wants_json() -> bool:
    """True when the caller expects a JSON response.

    Decided by what the caller asked for, not only by URL prefix. Admin
    fetch() calls live under /admin/* but send Accept/Content-Type JSON, and
    they cannot follow an HTML login redirect.
    """
    from flask import request
    if request.path.startswith("/api/"):
        return True
    if request.is_json:
        return True
    return "application/json" in (request.headers.get("Accept", "") or "")


def _unauthorized_json():
    from flask import jsonify
    return jsonify({"ok": False, "error": "Authentication required."}), 401


def _forbidden_json():
    from flask import jsonify
    return jsonify({"ok": False, "error": "Admin privileges required."}), 403
