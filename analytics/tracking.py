"""Privacy-preserving visitor analytics.

This module provides the Python helpers used by the analytics route and
the queue worker. The browser-side tracker is ``static/js/analytics.js``.

Privacy contract
----------------
- No raw IP address is ever stored.
- IPs are HMAC-SHA256'd with a server-side secret before being stored
  as ``ip_hmac`` (a 64-char hex string). The HMAC is pseudonymous —
  see ``docs/analytics-and-privacy.md`` for the legal caveats.
- No tool input, JSON/XML, JWTs, cookies, auth headers, or arbitrary
  request bodies are stored. Only small, allow-listed properties.
- Session identifiers are random first-party UUIDs generated in the
  browser; they are NOT cross-site and they are NOT fingerprints.
- When ``IP_HMAC_SECRET`` is missing, IP-derived fields are skipped
  entirely; analytics still works, but rate-limiting and country
  attribution degrade.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger("anvil.analytics")

# ── Constants ────────────────────────────────────────────────────────────────

ALLOWED_EVENT_NAMES = frozenset({
    "page_view",
    "tool_run",
    "tool_success",
    "tool_error",
    "query_submitted",
    "search",
    "feature_request_vote",
})

ALLOWED_DEVICE_TYPES = frozenset({"desktop", "mobile", "tablet", "unknown"})

COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# A hard cap on the number of keys/values in the JSONB ``properties`` column
# (pg_column_size <= 4096 in the schema). We pre-validate here so we never
# send a payload that the DB would reject.
MAX_PROPERTIES_BYTES = 4000

# Allow-list of property keys for tool-level events. Anything else is
# silently stripped before delivery to the worker.
ALLOWED_PROPERTY_KEYS = frozenset({
    "format",          # for formatter/converter tools
    "operation",       # formatter/validator/encoder/...
    "action",          # format/minify/validate/...
    "duration_ms",     # how long the tool took (integer)
    "input_size_bytes", # length of input — size only, never content
    "result_size_bytes",
    "outcome",         # "ok" or "error"
    "error_code",      # short string like "syntax_error"
    "vote_count",      # only for feature_request_vote
})

# Length caps so we never accidentally ship a giant string in the properties.
PROPERTY_STRING_CAP = 80
PROPERTY_INT_MAX = 2_000_000_000

# ── IP HMAC ──────────────────────────────────────────────────────────────────

def get_ip_hmac_secret() -> Optional[bytes]:
    """Return the configured HMAC secret or None.

    When None, IP-derived features are disabled and the browser is told
    not to send IP-derived identifiers (although the browser never sees
    the secret either way).
    """
    raw = os.environ.get("IP_HMAC_SECRET", "").strip()
    if not raw or len(raw) < 16:
        return None
    return raw.encode("utf-8")


def hash_ip(ip: Optional[str]) -> Optional[str]:
    """Return the 64-char hex HMAC of an IP, or None if disabled.

    The hash is keyed (HMAC-SHA256) rather than plain SHA-256 so an
    attacker who exfiltrates the database cannot brute-force the IP via a
    rainbow table. The secret must be at least 16 chars long.
    """
    if not ip:
        return None
    secret = get_ip_hmac_secret()
    if secret is None:
        return None
    digest = hmac.new(secret, ip.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


# ── Trusted proxy / client-IP extraction ────────────────────────────────────

# Operators can configure trusted proxy hops by setting
# ``TRUSTED_PROXY_COUNT`` (default: 1, assuming a single TLS-terminating
# proxy like Render's). We walk the X-Forwarded-For chain from the right
# and pick the first address that is NOT a trusted proxy.

TRUSTED_PRIVATE_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|::1$|fc|fd)",
    re.IGNORECASE,
)


def _is_private_ip(ip: str) -> bool:
    if not ip:
        return True
    return bool(TRUSTED_PRIVATE_RE.match(ip))


def trusted_proxy_count_from_env() -> int:
    """Number of trusted proxies that append to X-Forwarded-For.

    Defaults to 1 (a single TLS-terminating platform proxy such as Render).
    Set to 0 when the app is reached directly and X-Forwarded-For must be
    ignored entirely.
    """
    try:
        n = int(os.environ.get("TRUSTED_PROXY_COUNT", "1").strip())
    except ValueError:
        n = 1
    return max(0, min(n, 10))


def extract_client_ip(forwarded_for: Optional[str], remote_addr: Optional[str],
                      trusted_proxy_count: int = 1) -> Optional[str]:
    """Return the client IP that the trusted proxy chain vouches for.

    Convention: each trusted proxy APPENDS the address of its own peer to
    ``X-Forwarded-For``. The client-supplied prefix of the header is
    attacker-controlled, but the entries appended by our trusted proxies
    are not. With ``N`` trusted proxies, the true client is the entry
    ``N`` positions from the right: ``parts[-N]``.

    The previous implementation read ``parts[-(N+1)]``, one hop too far.
    That entry is the value the client sent, so an attacker could pick any
    IP and evade per-client rate limits (confirmed by reproduction).

    If the chain is shorter than the trusted-proxy count, the header cannot
    have come through our proxies, so we fall back to the TCP peer.
    """
    if trusted_proxy_count <= 0:
        return remote_addr or None
    if not forwarded_for:
        return remote_addr or None
    parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
    # Some proxies list their own address as the final entry. The TCP peer
    # (remote_addr) is trusted, so an identical final entry is the proxy
    # itself and is not counted as a hop. Platforms that list only the
    # client are unaffected.
    if parts and remote_addr and parts[-1] == remote_addr.strip():
        parts = parts[:-1]
    if len(parts) < trusted_proxy_count:
        return remote_addr or None
    candidate = parts[-trusted_proxy_count]
    if not _looks_like_ip(candidate):
        return remote_addr or None
    return candidate


# Per-process random key used ONLY for in-memory rate-limit buckets when no
# IP_HMAC_SECRET is configured. It is never persisted and never written to
# analytics tables, so it cannot leak identifiers. Keeping it random means
# rate limiting still works without configuration, and clients cannot choose
# their own bucket key (the old cookie fallback let them).
_EPHEMERAL_LIMITER_KEY = secrets.token_bytes(32)


def limiter_identity(forwarded_for: Optional[str], remote_addr: Optional[str]) -> str:
    """Return a stable, opaque rate-limit key for the requesting client.

    Keyed HMAC of the trusted-proxy-resolved client IP. Uses the configured
    ``IP_HMAC_SECRET`` when present, otherwise a per-process random key.
    The key is only used for in-memory rate limiting.
    """
    ip = extract_client_ip(forwarded_for, remote_addr, trusted_proxy_count_from_env()) or "unknown"
    key = get_ip_hmac_secret() or _EPHEMERAL_LIMITER_KEY
    digest = hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"ip:{digest}"


def _looks_like_ip(value: str) -> bool:
    if not value:
        return False
    if ":" in value and value.count(":") >= 2:
        # IPv6 — very loose check; postgres only stores the HMAC.
        return True
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit():
            return False
        n = int(p)
        if n < 0 or n > 255:
            return False
    return True


# ── Device-type detection ────────────────────────────────────────────────────

_DEVICE_RE = re.compile(
    r"(android|iphone|ipad|mobile|tablet)", re.IGNORECASE
)
_TABLET_RE = re.compile(r"(ipad|tablet)", re.IGNORECASE)


def detect_device_type(user_agent: Optional[str]) -> str:
    """Classify the device into one of the schema's CHECK-allowed values."""
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if _TABLET_RE.search(ua):
        return "tablet"
    if "android" in ua and "mobile" not in ua:
        # Many Android tablets send a UA without "mobile" — heuristic.
        return "tablet"
    if "mobile" in ua or "iphone" in ua:
        return "mobile"
    if "ipad" in ua:
        return "tablet"
    if _DEVICE_RE.search(ua):
        return "mobile"
    return "desktop"


# ── Referrer domain ─────────────────────────────────────────────────────────

def extract_referrer_domain(referrer: Optional[str]) -> Optional[str]:
    """Return the registered domain of the referrer, or None.

    The full URL is never stored — only the eTLD+1-ish host. This keeps
    query strings (which can carry UTM parameters and other PII) out
    of the database.
    """
    if not referrer:
        return None
    try:
        host = urlparse(referrer).hostname
    except Exception:
        return None
    if not host:
        return None
    if len(host) > 253:
        host = host[:253]
    return host


# ── Payload sanitization ────────────────────────────────────────────────────

def sanitize_properties(props: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Drop disallowed keys and bound string/int sizes.

    This is the last line of defense before the payload is queued for
    delivery. The DB has its own CHECK constraints, but failing them
    would mean a permanent dead-letter — better to drop fields here.
    """
    if not isinstance(props, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in props.items():
        if not isinstance(key, str) or key not in ALLOWED_PROPERTY_KEYS:
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            out[key] = max(-PROPERTY_INT_MAX, min(value, PROPERTY_INT_MAX))
        elif isinstance(value, float):
            out[key] = float(value)
        elif isinstance(value, str):
            out[key] = value[:PROPERTY_STRING_CAP]
        # Other types (lists, dicts, None) are dropped silently.
    # Final size check — return {} if we'd blow the JSONB column limit.
    import json
    encoded = json.dumps(out, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PROPERTIES_BYTES:
        log.warning("properties payload exceeded %d bytes after sanitization; dropped.",
                    MAX_PROPERTIES_BYTES)
        return {}
    return out


# ── Session validation ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class SessionInput:
    """A validated session payload received from the browser."""

    visitor_id: str
    session_id: str
    started_at: Optional[float]
    last_seen_at: Optional[float]
    engagement_seconds: int
    landing_page: Optional[str]
    referrer_domain: Optional[str]
    device_type: str
    country_code: Optional[str]

    def to_payload(self) -> Dict[str, Any]:
        """Render the payload as the worker expects it in the outbox."""
        return {
            "visitor_id": self.visitor_id,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "last_seen_at": self.last_seen_at,
            "engagement_seconds": self.engagement_seconds,
            "landing_page": self.landing_page,
            "referrer_domain": self.referrer_domain,
            "device_type": self.device_type,
            "country_code": self.country_code,
        }


def validate_session_payload(payload: Dict[str, Any]) -> Optional[SessionInput]:
    """Validate the session payload from the browser. Returns None on error.

    The browser generates ``visitor_id`` and ``session_id`` as random
    UUIDs. We accept them as-is after a regex check; we do NOT verify
    they match any server-side identity.
    """
    if not isinstance(payload, dict):
        return None
    visitor_id = payload.get("visitor_id") or ""
    session_id = payload.get("session_id") or ""
    if not _is_uuid(visitor_id) or not _is_uuid(session_id):
        return None

    engagement = payload.get("engagement_seconds") or 0
    if not isinstance(engagement, int) or isinstance(engagement, bool):
        try:
            engagement = int(engagement)
        except (TypeError, ValueError):
            engagement = 0
    # Schema CHECK: 0 <= engagement_seconds <= 604800 (7 days).
    engagement = max(0, min(engagement, 604800))

    device_type = payload.get("device_type") or "unknown"
    if device_type not in ALLOWED_DEVICE_TYPES:
        device_type = "unknown"

    country_code = (payload.get("country_code") or "").upper()
    if country_code and not COUNTRY_RE.match(country_code):
        country_code = None

    landing = payload.get("landing_page")
    if landing and (not isinstance(landing, str) or len(landing) > 2048):
        landing = landing[:2048] if isinstance(landing, str) else None

    referrer_domain = payload.get("referrer_domain")
    if referrer_domain and (not isinstance(referrer_domain, str)
                            or len(referrer_domain) > 253):
        referrer_domain = referrer_domain[:253] if isinstance(referrer_domain, str) else None

    return SessionInput(
        visitor_id=visitor_id,
        session_id=session_id,
        started_at=_to_epoch(payload.get("started_at")),
        last_seen_at=_to_epoch(payload.get("last_seen_at")),
        engagement_seconds=engagement,
        landing_page=landing,
        referrer_domain=referrer_domain,
        device_type=device_type,
        country_code=country_code or None,
    )


# ── Event validation ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EventInput:
    event_name: str
    session_id: str
    tool_slug: Optional[str]
    page_path: Optional[str]
    properties: Dict[str, Any]
    occurred_at: Optional[float]

    def to_payload(self, event_key: str) -> Dict[str, Any]:
        return {
            "event_key": event_key,
            "event_name": self.event_name,
            "session_id": self.session_id,
            "tool_slug": self.tool_slug,
            "page_path": self.page_path,
            "properties": self.properties,
            "occurred_at": self.occurred_at,
        }


def validate_event_payload(payload: Dict[str, Any]) -> Optional[EventInput]:
    if not isinstance(payload, dict):
        return None
    event_name = payload.get("event_name") or ""
    if event_name not in ALLOWED_EVENT_NAMES:
        return None
    session_id = payload.get("session_id") or ""
    if not _is_uuid(session_id):
        return None

    tool_slug = payload.get("tool_slug")
    if tool_slug and (not isinstance(tool_slug, str) or len(tool_slug) > 100):
        tool_slug = None

    page_path = payload.get("page_path")
    if page_path and (not isinstance(page_path, str) or len(page_path) > 2048):
        page_path = page_path[:2048] if isinstance(page_path, str) else None

    properties = sanitize_properties(payload.get("properties"))
    occurred_at = _to_epoch(payload.get("occurred_at"))
    return EventInput(
        event_name=event_name,
        session_id=session_id,
        tool_slug=tool_slug,
        page_path=page_path,
        properties=properties,
        occurred_at=occurred_at,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _is_uuid(value: str) -> bool:
    return bool(value) and bool(_UUID_RE.match(str(value)))


def _to_epoch(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def new_event_key() -> str:
    """Return a fresh UUID4 to use as an idempotency key."""
    return str(uuid.uuid4())


def now_epoch() -> float:
    return time.time()
