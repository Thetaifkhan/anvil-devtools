"""Regression tests for conversion defects found in the April audit pass.

Each test pins a behavior that previously produced wrong or misleading output.
"""
import json

import pytest

from services import conversion_service as conv
from services.errors import ToolError


def test_xml_error_line_number_is_correct_when_declaration_present():
    # The error is on line 3 of the user's input; the declaration must not shift it.
    result = conv.validate_xml('<?xml version="1.0"?>\n<root>\n<a></b>\n</root>')
    assert result["valid"] is False
    assert result["line"] == 3


def test_xml_error_line_number_unchanged_without_declaration():
    result = conv.validate_xml("<root>\n<a></b>\n</root>")
    assert result["line"] == 2


def test_xml_declaration_with_leading_whitespace_still_formats():
    out = conv.format_xml('  <?xml version="1.0" encoding="UTF-16"?>\n<a><b>1</b></a>')
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<b>1</b>" in out


def test_yaml_dates_become_iso_strings_not_errors():
    data = json.loads(conv.yaml_to_json("created: 2024-01-15\nstamp: 2001-12-14T21:59:43Z\n"))
    assert data["created"] == "2024-01-15"
    assert data["stamp"].startswith("2001-12-14T21:59:43")


def test_yaml_unsupported_type_error_does_not_blame_aliases():
    # A YAML binary value has no JSON equivalent; the message must not mention aliases.
    with pytest.raises(ToolError) as exc:
        conv.yaml_to_json("blob: !!binary aGVsbG8=\n")
    assert "recursive" not in exc.value.message
    assert "cannot be represented as JSON" in exc.value.message


def test_json_to_xml_renders_booleans_as_json_literals():
    result = conv.json_to_xml('{"ok": true, "off": false}')
    assert "<ok>true</ok>" in result
    assert "<off>false</off>" in result
    assert "True" not in result and "False" not in result


def test_csv_rejects_rows_with_extra_values_instead_of_null_key():
    with pytest.raises(ToolError) as exc:
        conv.csv_to_json("a,b\n1,2,3\n")
    assert "more values" in exc.value.message
    assert "line" in exc.value.message


def test_csv_ragged_short_rows_still_parse():
    data = json.loads(conv.csv_to_json("a,b\n1\n")[0])
    assert data == [{"a": "1", "b": None}]


def test_json_to_csv_writes_json_booleans_and_nulls():
    result = conv.json_to_csv('[{"flag": true, "gone": null, "n": 2}]')
    lines = result.strip().split("\r\n")
    assert lines[0] == "flag,gone,n"
    assert lines[1] == "true,,2"


def test_json_to_csv_keeps_nested_values_as_json_text():
    result = conv.json_to_csv('[{"meta": {"x": 1}, "tags": ["a", "b"]}]')
    # csv quoting doubles embedded quotes, so parse the row back to check the cells.
    import csv
    import io

    row = list(csv.reader(io.StringIO(result)))[1]
    assert json.loads(row[0]) == {"x": 1}
    assert json.loads(row[1]) == ["a", "b"]
    assert "{'x'" not in result
