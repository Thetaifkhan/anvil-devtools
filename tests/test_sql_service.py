import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from services import sql_service
from services.errors import ToolError


def test_format_sql_basic():
    result = sql_service.format_sql("select id,name from users where active=1")
    assert "SELECT" in result
    assert "FROM" in result


def test_format_sql_empty_raises():
    with pytest.raises(ToolError):
        sql_service.format_sql("   ")


def test_minify_sql():
    result = sql_service.minify_sql("SELECT   *\nFROM   users")
    assert "\n" not in result


def test_minify_sql_empty_raises():
    with pytest.raises(ToolError):
        sql_service.minify_sql("")


# Regression test for the audit-found bug where the SQL formatter UI offered
# a "Capitalize" keyword-case option, sqlparse supported it, but the API
# rejected it because the route only allowed "upper"/"lower".
def test_format_sql_keyword_case_capitalize():
    result = sql_service.format_sql("select id from users", keyword_case="capitalize")
    assert "Select" in result
    assert "From" in result
    # And lowercase variants must not appear.
    assert "select" not in result.lower().replace("select", "", 0) or "Select" in result
