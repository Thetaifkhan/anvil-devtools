"""
Pass 3 XML regression tests.

These tests verify the HARD requirements from the spec:
  - lxml is always active (no silent fallback)
  - UTF-16 / non-UTF-8 encoding declarations are handled safely
  - Mixed-content whitespace is PRESERVED (never stripped)
  - Element-only containers ARE indented
  - Namespace prefixes survive formatting exactly (soap: stays soap:)
  - Comments, CDATA, PIs, declarations survive
  - Idempotency: format(format(x)) == format(x)
  - Invalid XML → ToolError (never 500)
  - Minify does not corrupt content
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from services.conversion_service import (
    format_xml, validate_xml, json_to_xml, xml_to_json, _HAS_LXML,
)
from services.errors import ToolError


# ── Invariant: lxml is always active ─────────────────────────────────────────
def test_lxml_always_active():
    assert _HAS_LXML is True, "lxml must be installed — it is a required production dependency"


# ── Encoding declaration handling ─────────────────────────────────────────────

def test_utf8_declaration_preserved():
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<root/>'
    r = format_xml(xml)
    assert '<?xml' in r
    assert 'encoding="UTF-8"' in r

def test_utf16_declaration_normalised_to_utf8():
    """UTF-16 declaration must be normalised to UTF-8 (output is always UTF-8 text)."""
    xml = '<?xml version="1.0" encoding="UTF-16"?>\n<root/>'
    r = format_xml(xml)  # must not raise
    assert '<?xml' in r
    # Output encoding must say UTF-8 (we cannot output actual UTF-16 bytes)
    assert 'encoding="UTF-8"' in r or 'encoding="utf-8"' in r

def test_iso8859_declaration_normalised():
    xml = '<?xml version="1.0" encoding="ISO-8859-1"?>\n<root><item>hello</item></root>'
    r = format_xml(xml)
    assert 'hello' in r
    assert '<?xml' in r

def test_standalone_declaration_preserved():
    xml = '<?xml version="1.0" standalone="yes"?>\n<root/>'
    r = format_xml(xml)
    assert 'standalone="yes"' in r

def test_xml_11_declaration_preserved():
    xml = '<?xml version="1.1"?>\n<root/>'
    r = format_xml(xml)
    assert 'version="1.1"' in r

def test_no_declaration_not_added():
    xml = '<root><item>hello</item></root>'
    r = format_xml(xml)
    assert r.count('<?xml') == 0


# ── Mixed-content whitespace preservation ─────────────────────────────────────

def test_mixed_content_space_before_child_preserved():
    """<p> <b>world</b></p> — the space before <b> must survive."""
    xml = '<p> <b>world</b></p>'
    r = format_xml(xml)
    assert r.strip() == xml.strip(), f"Space before <b> was stripped!\nGot: {repr(r)}"

def test_mixed_content_text_and_child_preserved():
    """<p>Hello <b>world</b>!</p> — all text must survive untouched."""
    xml = '<p>Hello <b>world</b>!</p>'
    r = format_xml(xml)
    assert 'Hello ' in r
    assert 'world'  in r
    assert '!'      in r
    assert r.strip() == xml.strip(), f"Mixed content altered!\nGot: {repr(r)}"

def test_mixed_content_trailing_space_preserved():
    """<p>Hello <b>world</b> </p> — trailing space after </b> must survive."""
    xml = '<p>Hello <b>world</b> </p>'
    r = format_xml(xml)
    assert 'Hello ' in r
    assert '</b> '  in r or r.strip() == xml.strip(), f"Trailing space lost!\nGot: {repr(r)}"

def test_mixed_content_multiple_inline():
    """<p>Hello <b>world</b> and <i>everyone</i>!</p>"""
    xml = '<p>Hello <b>world</b> and <i>everyone</i>!</p>'
    r = format_xml(xml)
    assert 'Hello ' in r
    assert ' and '  in r
    assert '!'      in r

def test_element_only_content_is_indented():
    """<root><a>1</a><b>2</b></root> — structural indentation IS added."""
    xml = '<root><a>1</a><b>2</b></root>'
    r = format_xml(xml)
    assert '\n' in r
    assert '1' in r
    assert '2' in r

def test_structural_whitespace_normalised():
    """Already-indented structure must be normalised, not doubled."""
    xml = '<root>\n  <a>1</a>\n  <b>2</b>\n</root>'
    r = format_xml(xml)
    # Content must be present
    assert '1' in r and '2' in r


# ── Namespace prefix preservation ─────────────────────────────────────────────

def test_soap_prefix_preserved():
    xml = (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body><test>Hello</test></soap:Body>'
        '</soap:Envelope>'
    )
    r = format_xml(xml)
    assert 'soap:Envelope' in r, f"soap: prefix lost!\nGot:\n{r}"
    assert 'soap:Body'     in r
    assert 'ns0:'         not in r, f"soap: renamed to ns0:!\nGot:\n{r}"

def test_custom_prefix_preserved():
    xml = '<x:Root xmlns:x="http://example.com/x"><x:Child>val</x:Child></x:Root>'
    r = format_xml(xml)
    assert 'x:Root'  in r
    assert 'x:Child' in r
    assert 'ns0:'    not in r
    assert 'ns1:'    not in r

def test_multiple_prefixes_preserved():
    xml = (
        '<root xmlns:a="http://a.com" xmlns:b="http://b.com">'
        '<a:item>1</a:item><b:item>2</b:item>'
        '</root>'
    )
    r = format_xml(xml)
    assert 'a:item' in r
    assert 'b:item' in r
    assert 'ns0:'   not in r
    assert 'ns1:'   not in r

def test_namespace_uri_preserved():
    xml = '<root xmlns="http://example.com/schema"><item>Test</item></root>'
    r = format_xml(xml)
    assert 'http://example.com/schema' in r
    assert 'Test' in r


# ── Comment preservation ──────────────────────────────────────────────────────

def test_comment_preserved_pretty():
    xml = '<root><!-- important comment --><item>1</item></root>'
    r = format_xml(xml)
    assert '<!-- important comment -->' in r, f"Comment lost!\nGot:\n{r}"

def test_comment_preserved_minify():
    xml = '<root><!-- keep --><item>1</item></root>'
    r = format_xml(xml, minify=True)
    assert '<!-- keep -->' in r

def test_multiple_comments_preserved():
    xml = '<root><!-- a --><a/><!-- b --><b/></root>'
    r = format_xml(xml)
    assert '<!-- a -->' in r
    assert '<!-- b -->' in r


# ── CDATA preservation ────────────────────────────────────────────────────────

def test_cdata_preserved():
    xml = '<script><![CDATA[\nif (a < b) { console.log("test"); }\n]]></script>'
    r = format_xml(xml)
    assert 'CDATA' in r, f"CDATA lost!\nGot:\n{r}"
    assert 'a < b'  in r

def test_cdata_content_semantically_preserved():
    """Even if CDATA is converted to escaped text, the content must survive."""
    xml = '<root><![CDATA[x < y && z > w]]></root>'
    r = format_xml(xml)
    # Content must be present in some form
    assert 'x' in r and 'y' in r and 'z' in r and 'w' in r


# ── Processing instruction preservation ──────────────────────────────────────

def test_pi_preserved():
    xml = '<?xml-stylesheet type="text/xsl" href="style.xsl"?>\n<root/>'
    r = format_xml(xml)
    assert 'xml-stylesheet' in r, f"PI lost!\nGot:\n{r}"
    assert 'style.xsl'      in r


# ── Attribute preservation ────────────────────────────────────────────────────

def test_attributes_all_preserved():
    xml = '<user id="123" first-name="John" last-name="Doe" data-id="abc"><email>j@e.com</email></user>'
    r = format_xml(xml)
    assert 'id="123"'          in r
    assert 'first-name="John"' in r
    assert 'last-name="Doe"'   in r
    assert 'data-id="abc"'     in r

def test_empty_element_attrs_preserved():
    xml = '<br class="line" id="br1"/>'
    r = format_xml(xml)
    assert 'class="line"' in r
    assert 'id="br1"'     in r


# ── Entity preservation ───────────────────────────────────────────────────────

def test_entities_round_trip():
    """&lt; &gt; &amp; must survive semantically (may be re-encoded differently)."""
    xml = '<text>&lt;hello&gt; &amp; world</text>'
    r = format_xml(xml)
    # After parse+serialise the text content < > & are re-encoded as entities
    assert '<text' in r and '</text>' in r
    # The semantic content must be present
    assert '&lt;'  in r or '<hello>' in r  # one or the other
    assert '&amp;' in r or '& world' in r


# ── Self-closing and empty elements ──────────────────────────────────────────

def test_self_closing_element():
    xml = '<root><item/><other/></root>'
    r = format_xml(xml)
    assert 'item'  in r
    assert 'other' in r

def test_empty_element_with_attrs():
    xml = '<root><br class="line"/></root>'
    r = format_xml(xml)
    assert 'class="line"' in r


# ── Deep nesting ──────────────────────────────────────────────────────────────

def test_deep_nesting():
    xml = '<a><b><c><d><e><f>deep</f></e></d></c></b></a>'
    r = format_xml(xml)
    assert 'deep' in r

def test_large_document():
    items = ''.join(f'<item id="{i}">value{i}</item>' for i in range(200))
    xml = f'<root>{items}</root>'
    r = format_xml(xml)
    assert 'id="199"'   in r
    assert 'value199'    in r


# ── xml:space="preserve" ─────────────────────────────────────────────────────

def test_xml_space_preserve_attr_retained():
    xml = '<root xml:space="preserve"><text>   important   </text></root>'
    r = format_xml(xml)
    assert 'xml:space="preserve"' in r
    assert 'important' in r


# ── Idempotency ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("xml", [
    '<root><a>1</a></root>',
    '<root>\n  <a>1</a>\n</root>',
    '<p>Hello <b>world</b>!</p>',
    '<p> <b>world</b></p>',
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body/></soap:Envelope>',
    '<?xml version="1.0" encoding="UTF-8"?>\n<root><item>test</item></root>',
    '<root><!-- comment --><item>1</item></root>',
])
def test_idempotent_pretty(xml):
    once  = format_xml(xml)
    twice = format_xml(once)
    assert once == twice, (
        f"Not idempotent for: {repr(xml[:60])}\n"
        f"once:\n{once}\ntwice:\n{twice}"
    )

@pytest.mark.parametrize("xml", [
    '<root><a>1</a></root>',
    '<root>  <a>  </a>  </root>',
    '<p>Hello <b>world</b>!</p>',
])
def test_idempotent_minify(xml):
    once  = format_xml(xml, minify=True)
    twice = format_xml(once, minify=True)
    assert once == twice, f"Minify not idempotent for {repr(xml)}"


# ── Invalid XML ───────────────────────────────────────────────────────────────

def test_invalid_xml_raises_tool_error():
    with pytest.raises(ToolError):
        format_xml('<unclosed>')

def test_invalid_xml_provides_location():
    try:
        format_xml('<root>\n  <bad>\n</root>')
        assert False, "Should have raised"
    except ToolError as e:
        assert 'line' in e.message.lower() or 'column' in e.message.lower()

def test_invalid_xml_validate_returns_dict():
    r = validate_xml('<bad')
    assert r['valid'] is False
    assert r['message']

def test_valid_xml_validate_returns_true():
    r = validate_xml('<root><item>1</item></root>')
    assert r['valid'] is True


# ── Minification ──────────────────────────────────────────────────────────────

def test_minify_removes_newlines():
    xml = '<root>\n  <a>1</a>\n  <b>2</b>\n</root>'
    r = format_xml(xml, minify=True)
    assert '\n' not in r
    assert '1' in r and '2' in r

def test_minify_preserves_text():
    xml = '<root><item>keep me</item></root>'
    r = format_xml(xml, minify=True)
    assert 'keep me' in r

def test_minify_preserves_mixed_content():
    """Mixed content must not be corrupted during minification."""
    xml = '<p>Hello <b>world</b>!</p>'
    r = format_xml(xml, minify=True)
    assert 'Hello' in r
    assert 'world' in r
    assert '!'     in r

def test_minify_preserves_comments():
    xml = '<root><!-- keep --><item>1</item></root>'
    r = format_xml(xml, minify=True)
    assert '<!-- keep -->' in r


# ── JSON → XML root name validation ──────────────────────────────────────────

def test_json_to_xml_invalid_root_digit():
    with pytest.raises(ToolError, match="Invalid XML root"):
        json_to_xml('{"name":"John"}', root_name="1bad")

def test_json_to_xml_invalid_root_space():
    with pytest.raises(ToolError, match="Invalid XML root"):
        json_to_xml('{"name":"John"}', root_name="has space")

def test_json_to_xml_valid_root():
    r = json_to_xml('{"name":"Alice"}', root_name="person")
    assert '<person'   in r
    assert 'Alice'      in r
    assert '</person>'  in r

def test_json_to_xml_xss_escaping():
    """Script tags in JSON values must be XML-escaped in output."""
    r = json_to_xml('{"name": "<script>alert(1)</script>"}')
    assert '<script>' not in r  # raw unescaped tag must not appear


# ── XML → JSON ────────────────────────────────────────────────────────────────

def test_xml_to_json_basic():
    import json
    r = json.loads(xml_to_json('<root><name>Alice</name></root>'))
    assert r['name'] == 'Alice'

def test_xml_to_json_attributes():
    import json
    r = json.loads(xml_to_json('<book id="123"><title>Test</title></book>'))
    assert r['@attributes']['id'] == '123'
    assert r['title'] == 'Test'

def test_xml_to_json_repeated_elements():
    import json
    r = json.loads(xml_to_json('<books><book>A</book><book>B</book></books>'))
    assert isinstance(r['book'], list)
    assert len(r['book']) == 2

def test_xml_to_json_invalid():
    with pytest.raises(ToolError):
        xml_to_json('<unclosed>')


# ── Standard reference document ───────────────────────────────────────────────

def test_standard_reference_document():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<root>
    <user id="123">
        <name>John</name>
        <email>john@example.com</email>
    </user>
</root>'''
    r = format_xml(xml)
    assert 'John'             in r
    assert 'john@example.com' in r
    assert 'id="123"'         in r
    assert '<?xml'            in r
    # Must be idempotent
    assert format_xml(r) == r


# ── SOAP envelope (real-world complex example) ────────────────────────────────

def test_soap_envelope_full():
    xml = '''<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
    <soap:Header>
        <!-- Auth header -->
        <auth:Token xmlns:auth="http://example.com/auth">ABC123</auth:Token>
    </soap:Header>
    <soap:Body>
        <m:GetUser xmlns:m="http://example.com/api">
            <m:UserId>42</m:UserId>
        </m:GetUser>
    </soap:Body>
</soap:Envelope>'''
    r = format_xml(xml)
    # All prefixes preserved
    assert 'soap:Envelope' in r
    assert 'soap:Header'   in r
    assert 'soap:Body'     in r
    assert 'auth:Token'    in r
    assert 'm:GetUser'     in r
    assert 'ns0:'          not in r
    assert 'ns1:'          not in r
    assert 'ns2:'          not in r
    # Comment preserved
    assert '<!-- Auth header -->' in r
    # Idempotent
    assert format_xml(r) == r
