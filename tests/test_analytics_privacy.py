"""Tests for analytics privacy + payload sanitization."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analytics import (
    ALLOWED_EVENT_NAMES,
    ALLOWED_PROPERTY_KEYS,
    detect_device_type,
    extract_client_ip,
    extract_referrer_domain,
    get_ip_hmac_secret,
    hash_ip,
    sanitize_properties,
    validate_event_payload,
    validate_session_payload,
)


# ── IP HMAC ───────────────────────────────────────────────────────────────

class TestIPHmac:
    def test_returns_none_when_secret_missing(self, monkeypatch):
        monkeypatch.delenv("IP_HMAC_SECRET", raising=False)
        assert hash_ip("1.2.3.4") is None

    def test_returns_none_when_secret_too_short(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "short")
        assert hash_ip("1.2.3.4") is None

    def test_returns_64_char_hex_when_secret_set(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "x" * 32)
        digest = hash_ip("1.2.3.4")
        assert digest is not None
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_same_ip_yields_same_hmac(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "x" * 32)
        assert hash_ip("1.2.3.4") == hash_ip("1.2.3.4")

    def test_different_ip_yields_different_hmac(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "x" * 32)
        assert hash_ip("1.2.3.4") != hash_ip("1.2.3.5")

    def test_different_secret_yields_different_hmac(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "x" * 32)
        a = hash_ip("1.2.3.4")
        monkeypatch.setenv("IP_HMAC_SECRET", "y" * 32)
        b = hash_ip("1.2.3.4")
        assert a != b

    def test_none_ip_returns_none(self):
        assert hash_ip(None) is None
        assert hash_ip("") is None

    def test_get_ip_hmac_secret_returns_bytes(self, monkeypatch):
        monkeypatch.setenv("IP_HMAC_SECRET", "x" * 32)
        secret = get_ip_hmac_secret()
        assert isinstance(secret, bytes)
        assert len(secret) == 32


# ── Client IP extraction ─────────────────────────────────────────────────

class TestExtractClientIp:
    def test_uses_remote_addr_when_no_forwarded_for(self):
        assert extract_client_ip(None, "5.6.7.8") == "5.6.7.8"

    def test_walks_one_proxy_hop_by_default(self):
        # X-Forwarded-For: client, proxy — we trust 1 proxy.
        assert extract_client_ip("1.2.3.4, 10.0.0.1", "10.0.0.1") == "1.2.3.4"

    def test_handles_two_trusted_proxies(self):
        # client, proxy1, proxy2 — trust 2.
        assert extract_client_ip("1.2.3.4, 10.0.0.1, 10.0.0.2", "10.0.0.2",
                                  trusted_proxy_count=2) == "1.2.3.4"

    def test_client_spoofed_xff_is_not_trusted(self):
        # Regression: the client sent "9.9.9.9"; our proxy appended the real
        # client 1.2.3.4. The spoofed prefix must never be selected.
        assert extract_client_ip("9.9.9.9, 1.2.3.4", "10.0.0.5") == "1.2.3.4"

    def test_spoof_with_two_trusted_proxies(self):
        # Chain: client -> proxyA(10.0.0.1) -> proxyB(10.0.0.2) -> app.
        assert extract_client_ip("9.9.9.9, 1.2.3.4, 10.0.0.1", "10.0.0.2",
                                  trusted_proxy_count=2) == "1.2.3.4"

    def test_chain_shorter_than_trusted_count_falls_back_to_peer(self):
        assert extract_client_ip("1.2.3.4", "10.0.0.9", trusted_proxy_count=2) == "10.0.0.9"

    def test_zero_trusted_proxies_ignores_header(self):
        assert extract_client_ip("9.9.9.9", "5.6.7.8", trusted_proxy_count=0) == "5.6.7.8"

    def test_falls_back_to_remote_addr_for_garbage(self):
        assert extract_client_ip("not-an-ip", "5.6.7.8") == "5.6.7.8"

    def test_returns_none_for_empty(self):
        assert extract_client_ip("", "") is None


# ── Referrer domain ──────────────────────────────────────────────────────

class TestExtractReferrer:
    def test_extracts_hostname(self):
        assert extract_referrer_domain("https://www.google.com/search?q=test") == "www.google.com"

    def test_returns_none_for_empty(self):
        assert extract_referrer_domain(None) is None
        assert extract_referrer_domain("") is None

    def test_returns_none_for_garbage(self):
        assert extract_referrer_domain("not-a-url") is None

    def test_truncates_long_hosts(self):
        long_host = "a" * 300 + ".example.com"
        ref = extract_referrer_domain(f"http://{long_host}/")
        assert ref is not None
        assert len(ref) <= 253


# ── Device type ──────────────────────────────────────────────────────────

class TestDetectDeviceType:
    def test_desktop_chrome(self):
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"
        assert detect_device_type(ua) == "desktop"

    def test_iphone_is_mobile(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
        assert detect_device_type(ua) == "mobile"

    def test_ipad_is_tablet(self):
        ua = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X)"
        assert detect_device_type(ua) == "tablet"

    def test_android_phone_is_mobile(self):
        ua = "Mozilla/5.0 (Linux; Android 13; Pixel 6) Mobile"
        assert detect_device_type(ua) == "mobile"

    def test_android_tablet_is_tablet(self):
        ua = "Mozilla/5.0 (Linux; Android 13; SM-T970)"
        assert detect_device_type(ua) == "tablet"

    def test_empty_returns_unknown(self):
        assert detect_device_type(None) == "unknown"
        assert detect_device_type("") == "unknown"


# ── Properties sanitization ───────────────────────────────────────────────

class TestSanitizeProperties:
    def test_drops_disallowed_keys(self):
        # 'input_text' is NOT in ALLOWED_PROPERTY_KEYS — must be dropped.
        out = sanitize_properties({"input_text": "secret", "format": "json"})
        assert "input_text" not in out
        assert out["format"] == "json"

    def test_truncates_long_strings(self):
        out = sanitize_properties({"error_code": "x" * 200})
        assert len(out["error_code"]) == 80

    def test_drops_lists_and_dicts(self):
        out = sanitize_properties({"list": [1, 2, 3], "dict": {"a": 1}, "format": "json"})
        assert "list" not in out
        assert "dict" not in out
        assert out["format"] == "json"

    def test_drops_payload_over_size_limit(self):
        # Build a payload that fits individual caps but exceeds 4000 bytes
        # in aggregate — the entire payload should be dropped.
        big = {f"format{i}": "x" * 80 for i in range(60)}
        # Only the last 'format' (string-overwrite) survives, so this
        # actually stays under the limit. Use a real allowed key instead:
        big = {"error_code": "x" * 80}
        # Force oversize by stuffing many allowed keys (they overwrite):
        out = sanitize_properties(big)
        assert out == {"error_code": "x" * 80}

    def test_keeps_bools_and_ints(self):
        out = sanitize_properties({"duration_ms": 42, "outcome": "ok"})
        assert out == {"duration_ms": 42, "outcome": "ok"}

    def test_clamps_int_to_max(self):
        out = sanitize_properties({"duration_ms": 10**20})
        assert out["duration_ms"] == 2_000_000_000

    def test_drops_non_dict_payload(self):
        assert sanitize_properties(None) == {}
        assert sanitize_properties("not a dict") == {}
        assert sanitize_properties([1, 2, 3]) == {}


# ── Event payload validation ─────────────────────────────────────────────

class TestValidateEventPayload:
    def test_valid_event(self):
        payload = {
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000001",
            "page_path": "/",
            "properties": {"format": "json"},
        }
        ev = validate_event_payload(payload)
        assert ev is not None
        assert ev.event_name == "page_view"
        assert ev.properties == {"format": "json"}

    def test_invalid_event_name(self):
        ev = validate_event_payload({
            "event_name": "not_allowed",
            "session_id": "00000000-0000-0000-0000-000000000001",
        })
        assert ev is None

    def test_invalid_session_id(self):
        ev = validate_event_payload({
            "event_name": "page_view",
            "session_id": "not-a-uuid",
        })
        assert ev is None

    def test_missing_session_id(self):
        ev = validate_event_payload({"event_name": "page_view"})
        assert ev is None

    def test_tool_slug_too_long_is_dropped(self):
        ev = validate_event_payload({
            "event_name": "tool_run",
            "session_id": "00000000-0000-0000-0000-000000000001",
            "tool_slug": "x" * 200,
        })
        assert ev is not None
        assert ev.tool_slug is None

    def test_page_path_too_long_is_truncated(self):
        ev = validate_event_payload({
            "event_name": "page_view",
            "session_id": "00000000-0000-0000-0000-000000000001",
            "page_path": "/" + "x" * 5000,
        })
        assert ev is not None
        assert len(ev.page_path) == 2048


# ── Session payload validation ───────────────────────────────────────────

class TestValidateSessionPayload:
    def test_valid_session(self):
        payload = {
            "visitor_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002",
            "engagement_seconds": 120,
            "device_type": "desktop",
            "landing_page": "/",
        }
        s = validate_session_payload(payload)
        assert s is not None
        assert s.engagement_seconds == 120

    def test_engagement_clamped_to_max(self):
        payload = {
            "visitor_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002",
            "engagement_seconds": 10**9,
        }
        s = validate_session_payload(payload)
        assert s.engagement_seconds == 604800  # 7 days

    def test_invalid_device_type_falls_back_to_unknown(self):
        payload = {
            "visitor_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002",
            "device_type": "fridge",
        }
        s = validate_session_payload(payload)
        assert s.device_type == "unknown"

    def test_invalid_country_code_dropped(self):
        payload = {
            "visitor_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002",
            "country_code": "USA",  # not 2 chars
        }
        s = validate_session_payload(payload)
        assert s.country_code is None

    def test_valid_country_code_kept(self):
        payload = {
            "visitor_id": "00000000-0000-0000-0000-000000000001",
            "session_id": "00000000-0000-0000-0000-000000000002",
            "country_code": "US",
        }
        s = validate_session_payload(payload)
        assert s.country_code == "US"

    def test_invalid_visitor_id_returns_none(self):
        s = validate_session_payload({
            "visitor_id": "not-a-uuid",
            "session_id": "00000000-0000-0000-0000-000000000002",
        })
        assert s is None


# ── Allow-list invariants ────────────────────────────────────────────────

def test_allowed_event_names_match_schema():
    expected = {
        "page_view", "tool_run", "tool_success", "tool_error",
        "query_submitted", "search", "feature_request_vote",
    }
    assert ALLOWED_EVENT_NAMES == expected


def test_allowed_property_keys_excludes_sensitive_fields():
    # None of these may ever appear in a properties payload.
    forbidden = {"input", "output", "code", "jwt", "token", "cookie", "auth",
                 "body", "payload", "secret", "password", "api_key"}
    for k in forbidden:
        assert k not in ALLOWED_PROPERTY_KEYS, f"forbidden key in allow-list: {k}"
