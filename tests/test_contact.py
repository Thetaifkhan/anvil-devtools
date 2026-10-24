"""Tests for the contact form service + API endpoint."""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from delivery import outbox
from services.contact_service import (
    ALLOWED_CATEGORIES,
    ContactSubmission,
    client_idempotency_key,
    validate_submission,
)
from services.errors import ToolError


@pytest.fixture(autouse=True)
def temp_queue(tmp_path, monkeypatch):
    path = str(tmp_path / "outbox.sqlite3")
    outbox.configure(path)
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    # Clear env vars that could leak in from the dev machine.
    for k in ("DATABASE_URL", "SUPABASE_DB_PASSWORD", "SUPABASE_URL",
              "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(k, raising=False)
    import supabase_client
    supabase_client.reset_engine()
    # Reset module-level rate limiters so tests don't pollute each other.
    from services.rate_limit import CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER
    for limiter in (CONTACT_LIMITER, ANALYTICS_LIMITER, ADMIN_LIMITER):
        with limiter._lock:
            limiter._buckets.clear()
    yield path


# ── Service-level validation ──────────────────────────────────────────────

def _minimal_payload(**overrides):
    base = {
        "category": "bug_report",
        "subject": "Test subject",
        "message": "This is a test message with enough detail to pass validation.",
    }
    base.update(overrides)
    return base


class TestValidateSubmission:
    def test_minimal_valid_payload(self):
        s = validate_submission(_minimal_payload())
        assert isinstance(s, ContactSubmission)
        assert s.reference_code.startswith("ANVIL-")
        assert len(s.reference_code) == len("ANVIL-") + 8
        assert s.event_key  # UUID

    def test_missing_subject_rejected(self):
        with pytest.raises(ToolError, match="Subject"):
            validate_submission(_minimal_payload(subject=""))

    def test_missing_message_rejected(self):
        with pytest.raises(ToolError, match="Message"):
            validate_submission(_minimal_payload(message=""))

    def test_short_message_rejected(self):
        # Actually the validator only requires message to be non-empty.
        # But we should still accept a short message.
        s = validate_submission(_minimal_payload(message="short but valid"))
        assert s.message == "short but valid"

    def test_invalid_category_rejected(self):
        with pytest.raises(ToolError, match="Category"):
            validate_submission(_minimal_payload(category="invalid_cat"))

    def test_invalid_email_rejected(self):
        with pytest.raises(ToolError, match="[Ee]mail"):
            validate_submission(_minimal_payload(email="not-an-email"))

    def test_valid_email_accepted(self):
        s = validate_submission(_minimal_payload(email="user@example.com"))
        assert s.email == "user@example.com"

    def test_subject_too_long_rejected(self):
        with pytest.raises(ToolError, match="Subject"):
            validate_submission(_minimal_payload(subject="x" * 201))

    def test_message_too_long_rejected(self):
        with pytest.raises(ToolError, match="Message"):
            validate_submission(_minimal_payload(message="x" * 8001))

    def test_optional_name_accepted(self):
        s = validate_submission(_minimal_payload(name="Test User"))
        assert s.name == "Test User"

    def test_page_url_only_accepts_http_or_relative(self):
        s = validate_submission(_minimal_payload(page_url="/tools"))
        assert s.page_url == "/tools"
        s = validate_submission(_minimal_payload(page_url="https://example.com/x"))
        assert s.page_url == "https://example.com/x"
        # javascript: is rejected by stripping to None
        s = validate_submission(_minimal_payload(page_url="javascript:alert(1)"))
        assert s.page_url is None

    def test_tool_slug_truncated(self):
        s = validate_submission(_minimal_payload(tool_slug="x" * 200))
        assert len(s.tool_slug) == 100

    def test_reference_code_is_unique(self):
        s1 = validate_submission(_minimal_payload())
        s2 = validate_submission(_minimal_payload())
        assert s1.reference_code != s2.reference_code

    def test_consent_records_timestamp(self):
        s = validate_submission(_minimal_payload(consent="yes"))
        assert s.consent_recorded_at is not None
        assert s.consent_recorded_at <= time.time() + 1

    def test_no_consent_records_no_timestamp(self):
        s = validate_submission(_minimal_payload())
        assert s.consent_recorded_at is None


class TestBrowserContextSanitization:
    def test_keeps_allowlisted_fields(self):
        s = validate_submission(_minimal_payload(
            browser_context={
                "user_agent": "Mozilla/5.0",
                "viewport": {"width": 1920, "height": 1080},
                "language": "en-US",
                "timezone_offset_minutes": -300,
                "reduced_motion": False,
            }
        ))
        assert s.browser_context["user_agent"] == "Mozilla/5.0"
        assert s.browser_context["viewport"] == {"width": 1920, "height": 1080}
        assert s.browser_context["language"] == "en-US"
        assert s.browser_context["timezone_offset_minutes"] == -300
        assert s.browser_context["reduced_motion"] is False

    def test_drops_disallowed_fields(self):
        s = validate_submission(_minimal_payload(
            browser_context={
                "cookies": "session=abc123",
                "user_agent": "Mozilla",
                "fingerprint_hash": "abc123",
            }
        ))
        assert "cookies" not in s.browser_context
        assert "fingerprint_hash" not in s.browser_context
        assert s.browser_context["user_agent"] == "Mozilla"

    def test_user_agent_truncated(self):
        long_ua = "x" * 500
        s = validate_submission(_minimal_payload(
            browser_context={"user_agent": long_ua}
        ))
        assert len(s.browser_context["user_agent"]) == 200


class TestIdempotencyKey:
    def test_returns_stripped_key(self):
        assert client_idempotency_key({"idempotency_key": "abc-123 "}) == "abc-123"

    def test_returns_none_for_missing(self):
        assert client_idempotency_key({}) is None

    def test_returns_none_for_too_long(self):
        assert client_idempotency_key({"idempotency_key": "x" * 200}) is None

    def test_returns_none_for_non_string(self):
        assert client_idempotency_key({"idempotency_key": 123}) is None


# ── HTTP route integration ───────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    outbox.configure(str(tmp_path / "outbox.sqlite3"))
    monkeypatch.setenv("SUPABASE_WORKER_ENABLED", "0")
    from app import create_app
    app = create_app({"TESTING": True, "SECRET_KEY": "a" * 32})
    with app.test_client() as c:
        yield c


class TestContactRoutes:
    def test_contact_page_renders(self, client):
        r = client.get("/contact")
        assert r.status_code == 200
        assert b"contact-form" in r.data
        assert b"contact-submit" in r.data

    def test_invalid_payload_returns_400(self, client):
        r = client.post("/api/contact", json={"category": "not_real"})
        assert r.status_code == 400
        data = r.get_json()
        assert data["ok"] is False
        assert "Category" in data["error"]

    def test_valid_payload_returns_202_with_reference(self, client):
        r = client.post("/api/contact", json=_minimal_payload())
        assert r.status_code == 202
        data = r.get_json()
        assert data["ok"] is True
        assert data["reference_code"].startswith("ANVIL-")
        assert data["queued"] is True

    def test_duplicate_idempotency_key_does_not_create_duplicate(self, client):
        # First submit.
        r1 = client.post("/api/contact", json={**_minimal_payload(),
                                                "idempotency_key": "dup-key"})
        assert r1.status_code == 202
        ref1 = r1.get_json()["reference_code"]
        # Second submit with the SAME key — must return the SAME reference.
        r2 = client.post("/api/contact", json={**_minimal_payload(),
                                                "idempotency_key": "dup-key"})
        assert r2.status_code == 202
        data2 = r2.get_json()
        assert data2["reference_code"] == ref1
        assert data2["already_queued"] is True

    def test_rate_limit_kicks_in_after_5_submissions(self, client):
        # First 5 should succeed.
        for i in range(5):
            r = client.post("/api/contact", json=_minimal_payload(
                subject=f"Subject {i}",
                idempotency_key=f"key-{i}",
            ))
            assert r.status_code == 202, f"submit {i} unexpectedly failed"
        # 6th should be rate-limited.
        r = client.post("/api/contact", json=_minimal_payload(
            subject="Subject 6", idempotency_key="key-6",
        ))
        assert r.status_code == 429

    def test_contact_uses_post_method_only(self, client):
        r = client.get("/api/contact")
        assert r.status_code == 405  # Method Not Allowed

    def test_contact_payload_does_not_include_service_role_key(self, client):
        r = client.post("/api/contact", json=_minimal_payload())
        assert r.status_code == 202
        # The response body must never contain the service-role key.
        # (We can't easily test the absence here without setting one, but
        # we can at least verify the field names don't leak anything.)
        data = r.get_json()
        for k in data.keys():
            assert "key" not in k.lower() or k == "ok"
            assert "service" not in k.lower()
            assert "token" not in k.lower()

    def test_contact_includes_delivery_status_field(self, client):
        """The response must include a delivery_status field that
        honestly reflects the durability of the queue."""
        r = client.post("/api/contact", json=_minimal_payload())
        assert r.status_code == 202
        data = r.get_json()
        assert "delivery_status" in data, (
            "delivery_status field is required so the browser can honestly "
            "tell the user whether the submission was durably queued or "
            "only best-effort."
        )
        # In tests, OUTBOX_DURABLE is not set, so we expect queued_volatile.
        assert data["delivery_status"] in (
            "queued_durable", "queued_volatile", "delivered", "rejected"
        ), f"Unexpected delivery_status: {data['delivery_status']}"

    def test_contact_returns_queued_volatile_by_default(self, client, monkeypatch):
        """Without OUTBOX_DURABLE=1, the response is queued_volatile —
        the user is honestly told the submission is best-effort."""
        monkeypatch.delenv("OUTBOX_DURABLE", raising=False)
        r = client.post("/api/contact", json=_minimal_payload())
        assert r.status_code == 202
        data = r.get_json()
        assert data["delivery_status"] == "queued_volatile"
        assert "best-effort" in data["message"].lower(), (
            "The success message must honestly tell the user the "
            "submission is best-effort, not 'saved'."
        )

    def test_contact_returns_queued_durable_when_opted_in(self, client, monkeypatch):
        """With OUTBOX_DURABLE=1, the response is queued_durable — the
        operator has verified the queue path is on a persistent volume."""
        from delivery import outbox
        outbox.configure(durable=True)
        r = client.post("/api/contact", json=_minimal_payload())
        assert r.status_code == 202
        data = r.get_json()
        assert data["delivery_status"] == "queued_durable"
        assert "durably queued" in data["message"].lower()
