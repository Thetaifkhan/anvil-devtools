"""
Workbench action correctness tests (server-side).

Tests that the API endpoints used by the Workbench actually perform the
correct operations — i.e. Beautify beautifies, Minify minifies, Validate
validates.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as client:
        yield client


def post(client, endpoint, body):
    return client.post(
        endpoint,
        data=json.dumps(body),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# XML Beautify vs Minify — must produce different outputs
# ---------------------------------------------------------------------------

COMPACT_XML = "<root><item>1</item><item>2</item></root>"


def test_xml_beautify_adds_newlines(client):
    r = post(client, "/api/xml-format", {"text": COMPACT_XML, "minify": False})
    data = r.get_json()
    assert data["ok"] is True
    assert "\n" in data["result"]


def test_xml_minify_removes_newlines(client):
    pretty_xml = "<root>\n  <item>1</item>\n  <item>2</item>\n</root>"
    r = post(client, "/api/xml-format", {"text": pretty_xml, "minify": True})
    data = r.get_json()
    assert data["ok"] is True
    assert "\n" not in data["result"]
    assert "1" in data["result"]
    assert "2" in data["result"]


def test_xml_validate_valid(client):
    r = post(client, "/api/xml-validate", {"text": "<root><item/></root>"})
    data = r.get_json()
    assert data["ok"] is True
    assert data["valid"] is True


def test_xml_validate_invalid(client):
    r = post(client, "/api/xml-validate", {"text": "<root><unclosed>"})
    data = r.get_json()
    assert data["ok"] is True
    assert data["valid"] is False
    assert data["message"]


# ---------------------------------------------------------------------------
# YAML: both directions
# ---------------------------------------------------------------------------

YAML_DOC = "name: Alice\nage: 30\n"
JSON_DOC = '{"name": "Alice", "age": 30}'


def test_yaml_to_json_roundtrip(client):
    r = post(client, "/api/yaml-to-json", {"text": YAML_DOC})
    data = r.get_json()
    assert data["ok"] is True
    parsed = json.loads(data["result"])
    assert parsed["name"] == "Alice"
    assert parsed["age"] == 30


def test_json_to_yaml_roundtrip(client):
    r = post(client, "/api/json-to-yaml", {"text": JSON_DOC})
    data = r.get_json()
    assert data["ok"] is True
    assert "Alice" in data["result"]
    assert "age" in data["result"]


# ---------------------------------------------------------------------------
# SQL Beautify vs Minify
# ---------------------------------------------------------------------------

SQL = "select id,name from users where active=1"


def test_sql_beautify_uppercases_keywords(client):
    r = post(client, "/api/sql-format", {"text": SQL, "action": "format", "keyword_case": "upper"})
    data = r.get_json()
    assert data["ok"] is True
    assert "SELECT" in data["result"]
    assert "FROM" in data["result"]
    assert "WHERE" in data["result"]


def test_sql_minify_removes_whitespace(client):
    r = post(client, "/api/sql-format", {"text": SQL, "action": "minify"})
    data = r.get_json()
    assert data["ok"] is True
    assert "\n" not in data["result"]


def test_sql_minify_preserves_comments_by_default(client):
    sql_with_comment = "-- keep this\nSELECT 1;"
    r = post(client, "/api/sql-format", {"text": sql_with_comment, "action": "minify", "strip_comments": False})
    data = r.get_json()
    assert data["ok"] is True
    assert "keep this" in data["result"]


def test_sql_minify_strips_comments_when_opted_in(client):
    sql_with_comment = "-- remove this\nSELECT 1;"
    r = post(client, "/api/sql-format", {"text": sql_with_comment, "action": "minify", "strip_comments": True})
    data = r.get_json()
    assert data["ok"] is True
    assert "remove this" not in data["result"]


# ---------------------------------------------------------------------------
# JSON → XML root name validation
# ---------------------------------------------------------------------------

def test_json_to_xml_rejects_invalid_root_name(client):
    r = post(client, "/api/json-to-xml", {"text": '{"x":1}', "root": "1invalid"})
    data = r.get_json()
    assert data["ok"] is False
    assert "Invalid XML root" in data["error"]


def test_json_to_xml_accepts_valid_root_name(client):
    r = post(client, "/api/json-to-xml", {"text": '{"x":1}', "root": "myRoot"})
    data = r.get_json()
    assert data["ok"] is True
    assert "<myRoot" in data["result"]


# ---------------------------------------------------------------------------
# Stale output protection: errored request must not return a previous result
# ---------------------------------------------------------------------------

def test_xml_format_error_returns_no_result(client):
    r = post(client, "/api/xml-format", {"text": "<not valid xml at all>>>"})
    data = r.get_json()
    assert data["ok"] is False
    assert "result" not in data
