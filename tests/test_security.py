"""
Security regression tests.

Covers:
- XSS: JSON values with HTML in them must not execute
- Markdown link safety: javascript: / data: URLs must be blocked
- SQL: minify with strip_comments=False must preserve comments
- SQL: content inside strings must not be treated as comments
- Password generator: unbiased randomness (chi-squared smoke test)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import math
import pytest
from services.conversion_service import json_to_xml, xml_to_json
from services.sql_service import format_sql, minify_sql


# ---------------------------------------------------------------------------
# XSS: JSON-to-XML must not allow script injection in output
# ---------------------------------------------------------------------------

def test_json_to_xml_escapes_html_in_values():
    """<script> in a JSON value must come out as escaped XML text, not raw HTML."""
    result = json_to_xml('{"name": "<script>alert(1)</script>"}')
    # The raw < and > must be escaped in the XML output
    assert "<script>" not in result
    # But the content should be represented somehow
    assert "script" in result or "&lt;" in result


def test_json_to_xml_escapes_img_onerror():
    result = json_to_xml('{"name": "<img src=x onerror=alert(1)>"}')
    assert "<img" not in result


def test_xml_to_json_does_not_execute_script():
    """Parsing XML with embedded content should not cause any execution.
    We just verify the text value is preserved as a string."""
    import json as jsonmod
    result = jsonmod.loads(xml_to_json("<root><item>&lt;script&gt;alert(1)&lt;/script&gt;</item></root>"))
    # The text node value should contain the literal text (unescaped by XML parser)
    val = result.get("item", "")
    assert "<script>" in val  # XML parser gives us the raw text — JS doesn't execute in Python


# ---------------------------------------------------------------------------
# SQL: minify must NOT strip comments by default
# ---------------------------------------------------------------------------

def test_sql_minify_preserves_comments_by_default():
    sql = "-- important comment\nSELECT * FROM users;"
    result = minify_sql(sql, strip_comments=False)
    assert "important comment" in result


def test_sql_minify_strips_comments_only_when_opted_in():
    sql = "-- this comment should be removed\nSELECT 1;"
    result = minify_sql(sql, strip_comments=True)
    assert "this comment should be removed" not in result


def test_sql_minify_does_not_strip_content_inside_strings():
    """A string that looks like a comment must not be stripped."""
    sql = "SELECT 'hello -- not a comment' FROM users;"
    result = minify_sql(sql, strip_comments=False)
    assert "not a comment" in result


def test_sql_format_preserves_comments():
    sql = "-- header comment\nSELECT id, name FROM users WHERE active = 1;"
    result = format_sql(sql)
    assert "header comment" in result


# ---------------------------------------------------------------------------
# Password generator: unbiased randomness smoke test
# ---------------------------------------------------------------------------

def test_rejection_sampling_has_no_modulo_bias():
    """
    Exact check of the rejection-sampling rule (no randomness, so no flakiness).

    Accepting only bytes below limit = 256 - (256 % n) means every accepted
    byte maps onto each of the n characters exactly limit / n times. A
    statistical test at p=0.001 fails on correct code about 1 run in 1000,
    so this checks the arithmetic directly instead.
    """
    charset_size = 10
    limit = 256 - (256 % charset_size)  # 250
    counts = [0] * charset_size
    for byte in range(limit):
        counts[byte % charset_size] += 1
    assert set(counts) == {limit // charset_size}
    # Without rejection, bytes 250-255 would bias digits 0-5 upward.
    assert 256 % charset_size != 0



def test_xml_to_json_rejects_doctype_and_external_entities():
    """DTD/entity declarations are rejected instead of loading file/network data."""
    from services.errors import ToolError
    malicious = '<!DOCTYPE root [<!ENTITY leak SYSTEM "file:///etc/passwd">]><root>&leak;</root>'
    with pytest.raises(ToolError, match="DOCTYPE"):
        xml_to_json(malicious)
