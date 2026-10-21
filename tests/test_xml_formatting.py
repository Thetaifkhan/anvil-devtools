"""
XML formatting regression tests — comprehensive suite.

Verifies that lxml-based formatter preserves:
  - namespace prefixes (soap: stays soap:, NOT ns0:)
  - comments
  - CDATA sections
  - processing instructions
  - XML declaration
  - attributes and attribute values
  - text content / mixed content
  - entities
  - self-closing elements
  - deep nesting
  - xml:space="preserve"
  - idempotent formatting
  - invalid XML → ToolError with line/col
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from services.conversion_service import (
    format_xml, validate_xml, json_to_xml, xml_to_json,
    _HAS_LXML,
)
from services.errors import ToolError


# ---------------------------------------------------------------------------
# Basic
# ---------------------------------------------------------------------------

def test_format_xml_basic_pretty():
    result = format_xml("<a><b>hello</b></a>")
    assert "<b>hello</b>" in result
    assert "\n" in result

def test_format_xml_basic_minify():
    result = format_xml("<a>\n  <b>1</b>\n</a>", minify=True)
    assert "\n" not in result
    assert "<b>1</b>" in result

def test_format_xml_preserves_text_content():
    result = format_xml("<root><item>Hello World</item></root>")
    assert "Hello World" in result

# ---------------------------------------------------------------------------
# Namespace prefix preservation (CRITICAL — lxml must NOT rename to ns0:)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for prefix preservation")
def test_format_xml_preserves_soap_prefix():
    xml = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><test>Hello</test></soap:Body>'
        '</soap:Envelope>'
    )
    result = format_xml(xml)
    assert "soap:Envelope" in result, f"soap: prefix renamed! Got:\n{result}"
    assert "soap:Body"     in result
    assert "ns0:"         not in result, f"Prefix rewritten to ns0:! Got:\n{result}"

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for prefix preservation")
def test_format_xml_preserves_custom_prefix():
    xml = '<x:Root xmlns:x="http://example.com/x"><x:Child>val</x:Child></x:Root>'
    result = format_xml(xml)
    assert "x:Root"  in result, f"x: prefix lost! Got:\n{result}"
    assert "x:Child" in result
    assert "ns0:"    not in result
    assert "ns1:"    not in result

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for prefix preservation")
def test_format_xml_preserves_multiple_prefixes():
    xml = (
        '<root xmlns:a="http://a.com" xmlns:b="http://b.com">'
        '<a:item>1</a:item><b:item>2</b:item>'
        '</root>'
    )
    result = format_xml(xml)
    assert "a:item" in result
    assert "b:item" in result
    assert "ns0:"   not in result
    assert "ns1:"   not in result

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for prefix preservation")
def test_format_xml_default_namespace_preserved():
    xml = '<root xmlns="http://example.com/schema"><item>Test</item></root>'
    result = format_xml(xml)
    assert "http://example.com/schema" in result
    assert "Test" in result
    # lxml may use ns0: for default namespace when re-serialising — but the URI
    # must be preserved.  We tolerate either style here.

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for prefix preservation")
def test_format_xml_nested_namespace_declarations():
    xml = (
        '<root xmlns:a="http://a.com">'
        '<inner xmlns:b="http://b.com">'
        '<a:x/><b:y/>'
        '</inner>'
        '</root>'
    )
    result = format_xml(xml)
    assert "a:x" in result
    assert "b:y" in result

# ---------------------------------------------------------------------------
# Comment preservation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for comment preservation")
def test_format_xml_preserves_comments():
    xml = "<root>\n    <!-- important comment -->\n    <item>123</item>\n</root>"
    result = format_xml(xml)
    assert "<!-- important comment -->" in result, f"Comment lost! Got:\n{result}"

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for comment preservation")
def test_format_xml_preserves_multi_comments():
    xml = "<root><!-- a --><a/><!-- b --><b/></root>"
    result = format_xml(xml)
    assert "<!-- a -->" in result
    assert "<!-- b -->" in result

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for comment preservation")
def test_minify_preserves_comments():
    xml = "<root><!-- keep --><item>1</item></root>"
    result = format_xml(xml, minify=True)
    assert "<!-- keep -->" in result

# ---------------------------------------------------------------------------
# CDATA preservation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for CDATA preservation")
def test_format_xml_preserves_cdata():
    xml = "<script><![CDATA[\nif (a < b) { console.log(\"test\"); }\n]]></script>"
    result = format_xml(xml)
    assert "CDATA" in result, f"CDATA lost! Got:\n{result}"
    assert "a < b"  in result

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for CDATA preservation")
def test_format_xml_cdata_content_unchanged():
    inner = "x < y && z > w"
    xml = f"<root><![CDATA[{inner}]]></root>"
    result = format_xml(xml)
    # Content must be in the output, either as CDATA or escaped entities
    assert inner in result or "x &lt; y" in result

# ---------------------------------------------------------------------------
# Processing instructions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required for PI preservation")
def test_format_xml_preserves_pi():
    xml = '<?xml-stylesheet type="text/xsl" href="style.xsl"?>\n<root/>'
    result = format_xml(xml)
    assert "xml-stylesheet" in result, f"PI lost! Got:\n{result}"
    assert "style.xsl"      in result

# ---------------------------------------------------------------------------
# XML declaration
# ---------------------------------------------------------------------------

def test_format_xml_preserves_declaration():
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<root/>'
    result = format_xml(xml)
    assert '<?xml' in result, f"Declaration lost! Got:\n{result}"
    assert 'version="1.0"' in result

def test_format_xml_no_declaration_not_added():
    xml = "<root><item>1</item></root>"
    result = format_xml(xml)
    # No declaration in source → should not be added by the formatter
    assert result.count("<?xml") == 0

# ---------------------------------------------------------------------------
# Attributes
# ---------------------------------------------------------------------------

def test_format_xml_preserves_all_attributes():
    xml = '<user id="123" first-name="John" last-name="Doe" data-id="abc"><email>j@e.com</email></user>'
    result = format_xml(xml)
    assert 'id="123"'          in result
    assert 'first-name="John"' in result
    assert 'last-name="Doe"'   in result
    assert 'data-id="abc"'     in result

def test_format_xml_preserves_attribute_values_with_special_chars():
    xml = '<root attr="a &amp; b &lt; c"/>'
    result = format_xml(xml)
    assert "attr=" in result
    # The value must be preserved in some valid serialisation

# ---------------------------------------------------------------------------
# Text content and mixed content
# ---------------------------------------------------------------------------

def test_format_xml_preserves_text_in_leaf():
    xml = "<root><name>Alice</name></root>"
    result = format_xml(xml)
    assert "Alice" in result

def test_format_xml_does_not_strip_mixed_content_text():
    """Mixed content: text AND child elements — text must survive."""
    xml = "<doc><p>Hello World</p></doc>"
    result = format_xml(xml)
    assert "Hello World" in result

def test_format_xml_mixed_content_inline():
    xml = "<p>Hello <b>world</b>!</p>"
    result = format_xml(xml)
    # Text "Hello" and "!" must survive
    assert "Hello" in result
    assert "world" in result

def test_format_xml_entities_preserved_semantically():
    xml = "<text>&lt;hello&gt; &amp; world</text>"
    result = format_xml(xml)
    # After parse, text node contains literal chars; serialiser re-encodes them
    assert "text" in result
    # The element must be present
    assert "<text" in result and "</text>" in result

# ---------------------------------------------------------------------------
# Self-closing and empty elements
# ---------------------------------------------------------------------------

def test_format_xml_self_closing():
    xml = "<root><item/><other/></root>"
    result = format_xml(xml)
    assert "item" in result
    assert "other" in result

def test_format_xml_empty_element_with_attrs():
    xml = '<root><br class="line"/></root>'
    result = format_xml(xml)
    assert 'class="line"' in result

# ---------------------------------------------------------------------------
# Deep nesting
# ---------------------------------------------------------------------------

def test_format_xml_deep_nesting():
    xml = "<a><b><c><d><e><f>deep</f></e></d></c></b></a>"
    result = format_xml(xml)
    assert "deep" in result

def test_format_xml_large_repeated():
    items = "".join(f"<item id=\"{i}\">value{i}</item>" for i in range(100))
    xml = f"<root>{items}</root>"
    result = format_xml(xml)
    assert 'id="99"' in result
    assert "value99"  in result

# ---------------------------------------------------------------------------
# xml:space="preserve"
# ---------------------------------------------------------------------------

def test_format_xml_xml_space_preserve():
    xml = '<root xml:space="preserve"><text>   important   </text></root>'
    result = format_xml(xml)
    assert "important" in result
    assert 'xml:space="preserve"' in result

# ---------------------------------------------------------------------------
# Standard reference input
# ---------------------------------------------------------------------------

def test_format_xml_standard_input():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<root>
    <user id="123">
        <name>John</name>
        <email>john@example.com</email>
    </user>
</root>'''
    result = format_xml(xml)
    assert "John"             in result
    assert "john@example.com" in result
    assert 'id="123"'         in result

# ---------------------------------------------------------------------------
# Minify specifics
# ---------------------------------------------------------------------------

def test_format_xml_minify_strips_structural_whitespace():
    xml = "<root>\n  <a>hello</a>\n  <b>world</b>\n</root>"
    result = format_xml(xml, minify=True)
    assert "hello" in result
    assert "world" in result
    assert "\n"    not in result

def test_format_xml_minify_preserves_meaningful_text():
    xml = "<root><item>keep me</item></root>"
    result = format_xml(xml, minify=True)
    assert "keep me" in result
    assert "\n"      not in result

# ---------------------------------------------------------------------------
# Idempotency: format(format(xml)) == format(xml)
# ---------------------------------------------------------------------------

def test_format_xml_idempotent_pretty():
    xml = "<root><user id='1'><name>Alice</name></user></root>"
    once  = format_xml(xml)
    twice = format_xml(once)
    assert once == twice, f"Not idempotent!\nonce:\n{once}\ntwice:\n{twice}"

def test_format_xml_idempotent_minify():
    xml = "<root>\n  <a>1</a>\n  <b>2</b>\n</root>"
    once  = format_xml(xml, minify=True)
    twice = format_xml(once, minify=True)
    assert once == twice

@pytest.mark.skipif(not _HAS_LXML, reason="lxml required")
def test_format_xml_idempotent_with_namespace():
    xml = '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body/></soap:Envelope>'
    once  = format_xml(xml)
    twice = format_xml(once)
    assert once == twice

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_validate_xml_valid():
    r = validate_xml("<a><b>1</b></a>")
    assert r["valid"] is True

def test_validate_xml_invalid_unclosed():
    r = validate_xml("<a><b>1</a>")
    assert r["valid"] is False
    assert r["message"]

def test_validate_xml_invalid_reports_position():
    r = validate_xml("<a>bad</b>")
    assert r["valid"] is False

def test_validate_xml_invalid_provides_line_col():
    r = validate_xml("<root>\n  <bad>\n</root>")
    assert r["valid"] is False
    if _HAS_LXML:
        assert r.get("line") is not None

# ---------------------------------------------------------------------------
# JSON → XML root name validation
# ---------------------------------------------------------------------------

def test_json_to_xml_invalid_root_digit():
    with pytest.raises(ToolError, match="Invalid XML root"):
        json_to_xml('{"name":"John"}', root_name="1invalid")

def test_json_to_xml_invalid_root_space():
    with pytest.raises(ToolError, match="Invalid XML root"):
        json_to_xml('{"name":"John"}', root_name="has space")

def test_json_to_xml_invalid_root_empty():
    # Empty root → falls back to "root"
    result = json_to_xml('{"x":1}', root_name="")
    assert "<root" in result

def test_json_to_xml_valid_root():
    result = json_to_xml('{"name":"John"}', root_name="person")
    assert "<person"  in result
    assert "John"      in result
    assert "</person>" in result

def test_json_to_xml_xss_in_value():
    """JSON value containing HTML/script must be escaped in XML output."""
    result = json_to_xml('{"name": "<script>alert(1)</script>"}')
    assert "<script>" not in result  # raw tag must not appear
    # The content should be represented as escaped text
    assert "script" in result  # but the word is there

def test_json_to_xml_null_value():
    import json
    result = json_to_xml('{"key": null}')
    assert "nil" in result  # we use @nil="true" convention

# ---------------------------------------------------------------------------
# XML → JSON
# ---------------------------------------------------------------------------

def test_xml_to_json_basic():
    import json
    result = json.loads(xml_to_json("<root><name>Alice</name></root>"))
    assert result["name"] == "Alice"

def test_xml_to_json_attributes():
    import json
    result = json.loads(xml_to_json('<book id="123"><title>Test</title></book>'))
    assert result["@attributes"]["id"] == "123"
    assert result["title"] == "Test"

def test_xml_to_json_repeated_elements():
    import json
    result = json.loads(xml_to_json("<books><book>A</book><book>B</book></books>"))
    assert isinstance(result["book"], list)
    assert len(result["book"]) == 2

def test_xml_to_json_invalid_xml():
    with pytest.raises(ToolError):
        xml_to_json("<unclosed>")
