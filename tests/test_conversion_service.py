import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from services import conversion_service as conv
from services.errors import ToolError


def test_json_to_xml_basic():
    result = conv.json_to_xml('{"name": "John", "age": 30}')
    assert "<name>John</name>" in result
    assert "<age>30</age>" in result


def test_json_to_xml_array():
    result = conv.json_to_xml('{"skills": ["Python", "JavaScript"]}')
    assert result.count("<item>") == 2


def test_json_to_xml_invalid_json():
    with pytest.raises(ToolError):
        conv.json_to_xml('{invalid}')


def test_json_to_xml_sanitizes_tag_names():
    result = conv.json_to_xml('{"1key": "value", "bad key": "value2"}')
    assert "<_1key>" in result
    assert "<bad_key>" in result


def test_xml_to_json_roundtrip_basic():
    result = json.loads(conv.xml_to_json("<user><name>John</name><age>30</age></user>"))
    assert result["name"] == "John"
    assert result["age"] == "30"


def test_xml_to_json_attributes():
    result = json.loads(conv.xml_to_json('<book id="123"><title>Example</title></book>'))
    assert result["@attributes"]["id"] == "123"
    assert result["title"] == "Example"


def test_xml_to_json_repeated_elements_become_array():
    result = json.loads(conv.xml_to_json("<books><book>A</book><book>B</book></books>"))
    assert result["book"] == ["A", "B"]


def test_xml_to_json_invalid_xml():
    with pytest.raises(ToolError):
        conv.xml_to_json("<unclosed>")


def test_format_xml_pretty():
    result = conv.format_xml("<a><b>1</b></a>")
    assert "\n" in result
    assert "<b>1</b>" in result


def test_format_xml_minify():
    result = conv.format_xml("<a>\n  <b>1</b>\n</a>", minify=True)
    assert "\n" not in result


def test_validate_xml_valid():
    result = conv.validate_xml("<a><b>1</b></a>")
    assert result["valid"] is True


def test_validate_xml_invalid():
    result = conv.validate_xml("<a><b>1</a>")
    assert result["valid"] is False
    assert result["message"]


def test_json_to_yaml():
    result = conv.json_to_yaml('{"name": "John", "age": 30}')
    assert "name: John" in result
    assert "age: 30" in result


def test_yaml_to_json():
    result = json.loads(conv.yaml_to_json("name: John\nage: 30\n"))
    assert result["name"] == "John"
    assert result["age"] == 30


def test_yaml_to_json_invalid():
    with pytest.raises(ToolError):
        conv.yaml_to_json("key: [unclosed")


def test_csv_to_json():
    result, delim = conv.csv_to_json("name,age\nJohn,30\nSarah,28")
    data = json.loads(result)
    assert delim == ","
    assert data[0]["name"] == "John"
    assert data[1]["age"] == "28"


def test_csv_to_json_semicolon_delimiter():
    result, delim = conv.csv_to_json("name;age\nJohn;30")
    assert delim == ";"


def test_json_to_csv():
    result = conv.json_to_csv('[{"name":"John","age":30},{"name":"Sarah","age":28}]')
    lines = result.strip().split("\r\n")
    assert lines[0] == "name,age"
    assert "John,30" in lines[1]


def test_json_to_csv_requires_array_of_objects():
    with pytest.raises(ToolError):
        conv.json_to_csv('"just a string"')


# ---------------------------------------------------------------------------
# Regression tests for bugs found in the 2026-10 audit pass.
# ---------------------------------------------------------------------------

def test_xml_to_json_preserves_mixed_content_tail_text():
    # Regression: previously the tail text "!" was silently dropped
    # and only a boolean @mixed flag was emitted.
    result = json.loads(conv.xml_to_json('<p>Hello <b>world</b>!</p>'))
    assert result["#text"] == "Hello"
    assert result["b"] == "world"
    tails = result["#tails"]
    assert (tails if isinstance(tails, list) else [tails]) == ["!"]
    assert result["@mixed"] is True


def test_xml_to_json_preserves_namespace_prefix():
    # Regression: namespace prefixes were stripped along with default
    # namespace URIs, so a:item and b:item collapsed into a single "item" key.
    result = json.loads(conv.xml_to_json(
        '<root xmlns:a="http://a.com" xmlns:b="http://b.com">'
        '<a:item>x</a:item><b:item>y</b:item></root>'
    ))
    assert "a:item" in result
    assert "b:item" in result
    assert result["a:item"] == "x"
    assert result["b:item"] == "y"


def test_xml_to_json_strips_default_namespace():
    # Default namespaces (no prefix) have no prefix to preserve and should
    # collapse to the local name.
    result = json.loads(conv.xml_to_json('<root xmlns="http://default.com"><item>x</item></root>'))
    assert result["item"] == "x"


def test_json_to_xml_rejects_nan():
    with pytest.raises(ToolError, match="NaN"):
        conv.json_to_xml('{"x": NaN}')


def test_json_to_xml_rejects_infinity():
    with pytest.raises(ToolError, match="Infinity"):
        conv.json_to_xml('{"x": Infinity}')


def test_json_to_yaml_rejects_nan():
    with pytest.raises(ToolError, match="NaN"):
        conv.json_to_yaml('{"x": NaN}')


def test_json_to_csv_rejects_negative_infinity():
    with pytest.raises(ToolError, match="Infinity"):
        conv.json_to_csv('[{"x": -Infinity}]')


def test_xml_to_json_deep_nesting_no_libxml2_leak():
    # Build 500-level nested XML — exceeds libxml2's default 256 limit.
    nested = "<a>" * 500 + "</a>" * 500
    with pytest.raises(ToolError) as exc_info:
        conv.xml_to_json(f"<root>{nested}</root>")
    # The internal hint "use XML_PARSE_HUGE" must not leak to the user.
    assert "XML_PARSE_HUGE" not in str(exc_info.value)
    assert "Excessive depth" not in str(exc_info.value)
