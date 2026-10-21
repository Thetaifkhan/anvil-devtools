"""Tests for XML diff tool registration (BUG-02)."""
import pytest
from app import create_app
from tool_registry import get_tool, TOOLS


def test_xml_diff_tool_registered():
    """Verify XML diff tool is properly registered."""
    tool = get_tool("xml-diff")
    assert tool is not None
    assert tool["name"] == "XML Diff Checker"
    assert tool["operation"] == "diff"
    assert tool["formats"] == ["xml"]
    assert tool["template"] == "tools/xml_diff.html"


def test_xml_diff_page_loads():
    """Verify XML diff tool page can be loaded."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        resp = client.get("/xml-diff")
        assert resp.status_code == 200
        assert b"XML Diff Checker" in resp.data


def test_xml_diff_tool_in_tool_list():
    """Verify XML diff appears in the complete tool list."""
    tools = TOOLS
    slugs = [t["slug"] for t in tools]
    assert "xml-diff" in slugs


def test_xml_diff_in_category():
    """Verify XML diff is in the correct category."""
    tool = get_tool("xml-diff")
    assert tool["category"] == "JSON & XML"


def test_xml_diff_in_workbench_diff_operation():
    """Verify XML diff operation is available in workbench."""
    # The workbench should support XML comparison
    from tool_registry import CAPABILITY_MAP

    # Check that the diff operation includes xml format via json-diff and xml-diff
    all_diff_formats = set()
    for slug, (op, formats, actions) in CAPABILITY_MAP.items():
        if op == "diff":
            all_diff_formats.update(formats)

    # Should have text and json from existing tools, xml from new tool
    assert "text" in all_diff_formats
    assert "json" in all_diff_formats
    assert "xml" in all_diff_formats
