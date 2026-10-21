import sqlparse

from services.errors import ToolError


def format_sql(sql_text, indent_width=2, keyword_case="upper"):
    if not sql_text or not sql_text.strip():
        raise ToolError("Enter a SQL query to format.")
    try:
        return sqlparse.format(
            sql_text,
            reindent=True,
            keyword_case=keyword_case,
            indent_width=indent_width,
            strip_comments=False,  # Never silently remove comments
        )
    except Exception as exc:  # sqlparse rarely throws, but guard anyway
        raise ToolError(f"Could not format SQL: {exc}")


def minify_sql(sql_text, strip_comments=False):
    """Minify SQL. Comments are preserved by default — pass strip_comments=True
    only when the user has explicitly opted in."""
    if not sql_text or not sql_text.strip():
        raise ToolError("Enter a SQL query to minify.")
    return sqlparse.format(
        sql_text,
        strip_comments=strip_comments,
        strip_whitespace=True,
    )
