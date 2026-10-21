"""Tests for API input validation (BUG-22, BUG-24)."""
import json
import pytest
from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestAPIInputValidation:
    """Verify that APIs validate request types and reject invalid inputs."""

    def test_json_to_xml_missing_text_field(self, client):
        """Should reject request with missing 'text' field."""
        resp = client.post("/api/json-to-xml", json={})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "Missing required field: 'text'" in data["error"]

    def test_json_to_xml_text_not_string(self, client):
        """Should reject 'text' field that is not a string."""
        resp = client.post("/api/json-to-xml", json={"text": 123})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a string" in data["error"]

    def test_json_to_xml_text_is_array(self, client):
        """Should reject 'text' field that is an array."""
        resp = client.post("/api/json-to-xml", json={"text": ["not", "string"]})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a string" in data["error"]

    def test_json_to_xml_text_is_object(self, client):
        """Should reject 'text' field that is an object."""
        resp = client.post("/api/json-to-xml", json={"text": {"nested": "object"}})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a string" in data["error"]

    def test_json_to_xml_root_not_string(self, client):
        """Should reject 'root' field that is not a string."""
        resp = client.post("/api/json-to-xml", json={"text": "{}", "root": 123})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a string" in data["error"]

    def test_xml_format_minify_not_bool(self, client):
        """Should reject 'minify' field that is not a boolean."""
        resp = client.post("/api/xml-format", json={"text": "<root></root>", "minify": "true"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a boolean" in data["error"]

    def test_xml_format_rejects_unbounded_indent(self, client):
        resp = client.post("/api/xml-format", json={"text": "<root><item>x</item></root>", "indent": " " * 1000})
        assert resp.status_code == 400
        assert "0–16 spaces or one tab" in resp.get_json()["error"]

    def test_xml_format_rejects_control_characters_in_indent(self, client):
        resp = client.post("/api/xml-format", json={"text": "<root />", "indent": "\n"})
        assert resp.status_code == 400
        assert "0–16 spaces or one tab" in resp.get_json()["error"]

    def test_sql_format_invalid_action(self, client):
        """Should reject invalid action value."""
        resp = client.post("/api/sql-format", json={"text": "SELECT * FROM users;", "action": "invalid"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "Invalid action" in data["error"]

    def test_sql_format_invalid_keyword_case(self, client):
        """Should reject invalid keyword_case value."""
        resp = client.post("/api/sql-format", json={"text": "SELECT * FROM users;", "keyword_case": "Mixed"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "Invalid keyword_case" in data["error"]

    def test_sql_format_indent_width_not_int(self, client):
        """Should reject non-integer indent_width."""
        resp = client.post("/api/sql-format", json={"text": "SELECT * FROM users;", "indent_width": "2"})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be an integer" in data["error"]

    def test_sql_format_indent_width_out_of_range(self, client):
        """Should reject indent_width outside valid range."""
        resp = client.post("/api/sql-format", json={"text": "SELECT * FROM users;", "indent_width": 20})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "between 0 and 16" in data["error"]

    def test_csv_to_json_delimiter_not_string(self, client):
        """Should reject non-string delimiter."""
        resp = client.post("/api/csv-to-json", json={"text": "a,b,c\n1,2,3", "delimiter": 123})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "must be a string" in data["error"]

    def test_valid_json_to_xml(self, client):
        """Should accept valid JSON to XML conversion."""
        resp = client.post("/api/json-to-xml", json={"text": '{"root":"value"}'})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "<root>value</root>" in data["result"]

    def test_valid_sql_format(self, client):
        """Should accept valid SQL formatting."""
        resp = client.post("/api/sql-format", json={"text": "SELECT * FROM users;"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "SELECT" in data["result"]

    def test_sql_format_accepts_capitalize_keyword_case(self, client):
        """Regression: BUG-28 — the UI offers 'Capitalize' but the API used
        to reject it. The route now allows 'upper'/'lower'/'capitalize'."""
        resp = client.post("/api/sql-format", json={
            "text": "select id from users",
            "action": "format",
            "keyword_case": "capitalize",
        })
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True
        assert "Select" in data["result"]
        assert "From" in data["result"]

    def test_sql_format_rejects_unknown_keyword_case(self, client):
        """Reject keyword_case values that aren't upper/lower/capitalize."""
        resp = client.post("/api/sql-format", json={
            "text": "select 1",
            "action": "format",
            "keyword_case": "title-case",
        })
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "keyword_case" in data["error"]


class TestAPISizeLimits:
    """Verify that APIs enforce size limits."""

    def test_oversized_text_input(self, client):
        """Should reject text input exceeding 2MB limit."""
        oversized = "x" * (2_000_001)
        resp = client.post("/api/json-to-xml", json={"text": oversized})
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "too large" in data["error"].lower()
    def test_non_object_json_body_is_rejected(self, client):
        resp = client.post("/api/json-to-xml", json=["text", "{}"]) 
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data["ok"] is False
        assert "JSON object" in data["error"]

    def test_configured_text_limit_is_used(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-only-key", "MAX_TEXT_CHARS": 4})
        with app.test_client() as client:
            resp = client.post("/api/json-to-xml", json={"text": "12345"})
        assert resp.status_code == 400
        assert "4 characters" in resp.get_json()["error"]

    def test_security_headers_and_api_no_store(self, client):
        page = client.get("/")
        assert page.headers.get("X-Content-Type-Options") == "nosniff"
        assert page.headers.get("X-Frame-Options") == "DENY"
        assert "frame-ancestors 'none'" in page.headers.get("Content-Security-Policy", "")
        api = client.post("/api/json-to-xml", json={"text": "{}"})
        assert api.headers.get("Cache-Control") == "no-store"

