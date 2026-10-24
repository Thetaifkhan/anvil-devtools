"""Contact form service.

Validates, normalizes, and queues contact submissions for durable
delivery to ``public.support_queries``. The route handler in
:mod:`routes.contact` is thin — all the real logic lives here so the
unit tests can drive it directly without spinning up Flask.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from services.errors import ToolError

log = logging.getLogger("anvil.contact")

# ── Constants ────────────────────────────────────────────────────────────────

ALLOWED_CATEGORIES = (
    "bug_report",
    "feature_request",
    "general_question",
    "feedback",
    "other",
)

MAX_NAME = 200
MAX_EMAIL = 320
MAX_SUBJECT = 200
MAX_MESSAGE = 8000
MAX_PAGE_URL = 2048
MAX_TOOL_SLUG = 100

# Reference code is human-friendly: ANVIL-XXXXXXXX (8 base32 chars).
# ~5 bytes of entropy → ~2^40 collision space; we still rely on the DB
# unique index in migration 004 to make collisions safe.
REFERENCE_PREFIX = "ANVIL"
REFERENCE_LEN = 8

_REFERENCE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I/L/O/0/1


# ── Validation ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContactSubmission:
    """A validated contact form submission."""

    reference_code: str
    name: Optional[str]
    email: Optional[str]
    category: str
    subject: str
    message: str
    page_url: Optional[str]
    tool_slug: Optional[str]
    browser_context: Dict[str, Any] = field(default_factory=dict)
    consent_recorded_at: Optional[float] = None
    event_key: str = field(default_factory=str)
    created_at: float = field(default_factory=time.time)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "reference_code": self.reference_code,
            "name": self.name,
            "email": self.email,
            "category": self.category,
            "subject": self.subject,
            "message": self.message,
            "page_url": self.page_url,
            "tool_slug": self.tool_slug,
            "browser_context": self.browser_context,
            "consent_recorded_at": self.consent_recorded_at,
            "occurred_at": self.created_at,
        }


def validate_submission(payload: Dict[str, Any]) -> ContactSubmission:
    """Validate a contact form payload.

    Raises :class:`ToolError` with a friendly message on any failure. The
    route handler turns these into 400 responses with the message shown
    to the user.
    """
    if not isinstance(payload, dict):
        raise ToolError("Request body must be a JSON object.")

    category = (payload.get("category") or "").strip()
    if category not in ALLOWED_CATEGORIES:
        raise ToolError(
            f"Category must be one of: {', '.join(ALLOWED_CATEGORIES)}."
        )

    subject = (payload.get("subject") or "").strip()
    if not subject:
        raise ToolError("Subject is required.")
    if len(subject) > MAX_SUBJECT:
        raise ToolError(f"Subject must be at most {MAX_SUBJECT} characters.")

    message = (payload.get("message") or "").strip()
    if not message:
        raise ToolError("Message is required.")
    if len(message) > MAX_MESSAGE:
        raise ToolError(f"Message must be at most {MAX_MESSAGE} characters.")

    name = (payload.get("name") or "").strip() or None
    if name and len(name) > MAX_NAME:
        raise ToolError(f"Name must be at most {MAX_NAME} characters.")

    email = (payload.get("email") or "").strip() or None
    if email:
        if len(email) > MAX_EMAIL or "@" not in email or " " in email:
            raise ToolError("Email address looks invalid.")
        local, _, domain = email.rpartition("@")
        if not local or not domain or "." not in domain:
            raise ToolError("Email address looks invalid.")

    page_url = (payload.get("page_url") or "").strip() or None
    if page_url:
        if len(page_url) > MAX_PAGE_URL:
            page_url = page_url[:MAX_PAGE_URL]
        if not page_url.startswith(("http://", "https://", "/")):
            # Allow relative URLs only (they came from our own page).
            page_url = None

    tool_slug = (payload.get("tool_slug") or "").strip() or None
    if tool_slug and len(tool_slug) > MAX_TOOL_SLUG:
        tool_slug = tool_slug[:MAX_TOOL_SLUG]

    browser_context_raw = payload.get("browser_context") or {}
    browser_context = _sanitize_browser_context(browser_context_raw)

    consent = payload.get("consent")
    consent_recorded_at = None
    if consent:
        # Truthy consent field (any string/bool) records a timestamp.
        consent_recorded_at = time.time()

    # Generate the reference code and the idempotency event_key.
    reference_code = _generate_reference_code()
    event_key = str(uuid.uuid4())

    return ContactSubmission(
        reference_code=reference_code,
        name=name,
        email=email,
        category=category,
        subject=subject,
        message=message,
        page_url=page_url,
        tool_slug=tool_slug,
        browser_context=browser_context,
        consent_recorded_at=consent_recorded_at,
        event_key=event_key,
    )


def _generate_reference_code() -> str:
    """Generate a collision-resistant human-readable code.

    Format: ``ANVIL-XXXXXXXX`` where X is from a 30-char alphabet
    (no I/L/O/0/1 to avoid transcription confusion). The DB unique
    index guarantees correctness even in the rare collision case.
    """
    # 5 random bytes → 40 bits → ~1e12 codes. The DB unique index is the
    # backstop for collisions; the app regenerates on conflict.
    rand_bytes = secrets.token_bytes(5)
    n = int.from_bytes(rand_bytes, "big")
    out = []
    base = len(_REFERENCE_ALPHABET)
    for _ in range(REFERENCE_LEN):
        n, r = divmod(n, base)
        out.append(_REFERENCE_ALPHABET[r])
    code = "".join(out)
    return f"{REFERENCE_PREFIX}-{code}"


def _sanitize_browser_context(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Drop anything that isn't a small allow-listed diagnostic.

    We keep only:
    - ``user_agent`` (truncated to 200 chars)
    - ``viewport`` ({width, height})
    - ``language`` (truncated to 30 chars)
    - ``timezone_offset_minutes`` (int)
    - ``reduced_motion`` (bool)

    Anything else (cookies, plugins, full fingerprint fields) is dropped.
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    ua = raw.get("user_agent")
    if isinstance(ua, str):
        out["user_agent"] = ua[:200]
    viewport = raw.get("viewport")
    if isinstance(viewport, dict):
        w = viewport.get("width")
        h = viewport.get("height")
        if isinstance(w, int) and isinstance(h, int):
            out["viewport"] = {"width": max(0, min(w, 9999)),
                               "height": max(0, min(h, 9999))}
    lang = raw.get("language")
    if isinstance(lang, str):
        out["language"] = lang[:30]
    tzo = raw.get("timezone_offset_minutes")
    if isinstance(tzo, int) and not isinstance(tzo, bool):
        out["timezone_offset_minutes"] = max(-1440, min(tzo, 1440))
    rm = raw.get("reduced_motion")
    if isinstance(rm, bool):
        out["reduced_motion"] = rm
    # Hard cap on JSON size — DB CHECK allows 4096 bytes per the schema.
    import json
    encoded = json.dumps(out, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 4000:
        return {}
    return out


# ── Idempotency helpers ─────────────────────────────────────────────────────

def client_idempotency_key(payload: Dict[str, Any]) -> Optional[str]:
    """Return the client-supplied idempotency key from the request, if any.

    Used to detect duplicate-submit clicks: the browser sends the same
    key for the same form attempt so a network-retried submit doesn't
    produce two rows. We do NOT trust this key alone — we also generate
    a server-side event_key.
    """
    raw = payload.get("idempotency_key") or ""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or len(raw) > 128:
        return None
    # Allow only UUIDs and our own ANVIL-XXXXXXXX format.
    return raw
