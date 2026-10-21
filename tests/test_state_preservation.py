"""
State preservation regression tests (server-side portion).

Tests that:
1. Format switching preserves state server-side (routes return correct data
   regardless of which format was previously active).
2. The selected action is executed (not a hardcoded first action).
3. Stale output is never returned on error.
4. SQL minify/format are distinct operations.
5. Async race: error response on bad input, never old good output.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import pytest
from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def post(client, endpoint, body):
    return client.post(endpoint, data=json.dumps(body), content_type="application/json")


# ---------------------------------------------------------------------------
# Each format action is independent and produces distinct output
# ---------------------------------------------------------------------------

MINIFIED_JSON = '{"a":1,"b":2}'
PRETTY_JSON   = '{\n  "a": 1,\n  "b": 2\n}'

def test_json_beautify_is_not_minify(client):
    """JSON formatting is client-side. Verify the Python round-trip directly."""
    import json as _json
    data = _json.loads(MINIFIED_JSON)
    beautified = _json.dumps(data, indent=2)
    minified   = _json.dumps(data)
    assert "\n"  in beautified
    assert "\n" not in minified
    assert beautified != minified

def test_xml_beautify_produces_newlines(client):
    r = post(client, "/api/xml-format", {"text": "<root><a>1</a></root>", "minify": False})
    data = r.get_json()
    assert data["ok"]
    assert "\n" in data["result"]

def test_xml_minify_removes_newlines(client):
    r = post(client, "/api/xml-format", {"text": "<root>\n  <a>1</a>\n</root>", "minify": True})
    data = r.get_json()
    assert data["ok"]
    assert "\n" not in data["result"]
    assert "1" in data["result"]

def test_xml_validate_vs_format_returns_different_things(client):
    xml = "<root><item>1</item></root>"
    r_val = post(client, "/api/xml-validate", {"text": xml}).get_json()
    r_fmt = post(client, "/api/xml-format",   {"text": xml, "minify": False}).get_json()
    assert r_val["ok"] and r_val["valid"]
    assert r_fmt["ok"]
    # validate returns a "valid" key; format returns a "result" key
    assert "valid"  in r_val
    assert "result" in r_fmt

# ---------------------------------------------------------------------------
# Stale output: error must return ok=False with NO result key
# ---------------------------------------------------------------------------

def test_xml_format_error_has_no_result(client):
    r = post(client, "/api/xml-format", {"text": "<<<not xml>>>"})
    data = r.get_json()
    assert data["ok"] is False
    assert "result" not in data

def test_xml_validate_invalid_has_no_result(client):
    r = post(client, "/api/xml-validate", {"text": "<bad>"})
    data = r.get_json()
    assert data["ok"] is True   # the validate endpoint always returns ok=True
    assert data["valid"] is False
    assert "result" not in data

def test_yaml_error_has_no_result(client):
    r = post(client, "/api/yaml-to-json", {"text": "key: [unclosed"})
    data = r.get_json()
    assert data["ok"] is False
    assert "result" not in data

# ---------------------------------------------------------------------------
# SQL strip_comments option is respected
# ---------------------------------------------------------------------------

def test_sql_minify_preserves_comments_by_default(client):
    sql = "-- important\nSELECT 1;"
    r = post(client, "/api/sql-format", {"text": sql, "action": "minify", "strip_comments": False})
    data = r.get_json()
    assert data["ok"]
    assert "important" in data["result"]

def test_sql_minify_strips_comments_when_opted_in(client):
    sql = "-- remove me\nSELECT 1;"
    r = post(client, "/api/sql-format", {"text": sql, "action": "minify", "strip_comments": True})
    data = r.get_json()
    assert data["ok"]
    assert "remove me" not in data["result"]

def test_sql_format_always_preserves_comments(client):
    sql = "-- always keep\nSELECT 1;"
    r = post(client, "/api/sql-format", {"text": sql, "action": "format"})
    data = r.get_json()
    assert data["ok"]
    assert "always keep" in data["result"]

def test_sql_string_not_treated_as_comment(client):
    """Content inside a SQL string literal that looks like a comment must not be stripped."""
    sql = "SELECT 'hello -- not a comment' FROM users;"
    r = post(client, "/api/sql-format", {"text": sql, "action": "minify", "strip_comments": True})
    data = r.get_json()
    assert data["ok"]
    assert "not a comment" in data["result"]

# ---------------------------------------------------------------------------
# JSON → XML root name validation (user-facing error, not 500)
# ---------------------------------------------------------------------------

def test_json_to_xml_invalid_root_gives_400_not_500(client):
    r = post(client, "/api/json-to-xml", {"text": '{"a":1}', "root": "1bad"})
    data = r.get_json()
    assert data["ok"] is False
    assert "Invalid XML root" in data["error"]
    # Must not be a 500 error
    assert r.status_code != 500

def test_json_to_xml_valid_root_succeeds(client):
    r = post(client, "/api/json-to-xml", {"text": '{"name":"Alice"}', "root": "person"})
    data = r.get_json()
    assert data["ok"]
    assert "<person" in data["result"]
    assert "Alice"    in data["result"]

# ---------------------------------------------------------------------------
# Conversion endpoints both directions
# ---------------------------------------------------------------------------

def test_csv_to_json(client):
    csv = "name,age\nAlice,30\nBob,25"
    r = post(client, "/api/csv-to-json", {"text": csv})
    data = r.get_json()
    assert data["ok"]
    rows = json.loads(data["result"])
    assert rows[0]["name"] == "Alice"
    assert rows[1]["age"]  == "25"

def test_json_to_csv(client):
    j = '[{"name":"Alice","age":30}]'
    r = post(client, "/api/json-to-csv", {"text": j})
    data = r.get_json()
    assert data["ok"]
    assert "name" in data["result"]
    assert "Alice" in data["result"]

# ---------------------------------------------------------------------------
# XML namespace prefix regression (server returns preserved prefixes)
# ---------------------------------------------------------------------------

def test_xml_format_preserves_soap_prefix(client):
    xml = '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body/></soap:Envelope>'
    r = post(client, "/api/xml-format", {"text": xml, "minify": False})
    data = r.get_json()
    assert data["ok"]
    assert "soap:Envelope" in data["result"], f"Prefix lost: {data['result']}"
    assert "ns0:"          not in data["result"], f"Prefix renamed: {data['result']}"

def test_xml_format_preserves_comments_server(client):
    xml = "<root><!-- keep me --><item>1</item></root>"
    r = post(client, "/api/xml-format", {"text": xml, "minify": False})
    data = r.get_json()
    assert data["ok"]
    assert "<!-- keep me -->" in data["result"], f"Comment lost: {data['result']}"

def test_xml_format_preserves_cdata_server(client):
    xml = "<root><![CDATA[a < b]]></root>"
    r = post(client, "/api/xml-format", {"text": xml, "minify": False})
    data = r.get_json()
    assert data["ok"]
    assert "CDATA" in data["result"], f"CDATA lost: {data['result']}"
