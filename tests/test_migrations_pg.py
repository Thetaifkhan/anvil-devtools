"""Validate every SQL migration file against the real PostgreSQL parser.

Uses ``pglast`` (a Python binding around the actual libpg_query C library
that ships with PostgreSQL) to parse each migration file. A successful
parse means PostgreSQL itself would accept the SQL syntactically. (It
cannot verify the migration succeeds against a specific schema, only
that the SQL is well-formed.)

This test was added in response to issues found during validation of
anvil-devtools-complete.zip:
- The original ``private.claim_delivery_queue`` used ``UPDATE ...
  RETURNING`` directly inside a ``RETURNS TABLE`` function body, which
  compiles but does NOT return rows to the caller (the rows are
  silently discarded). The fix uses ``RETURN QUERY UPDATE ... RETURNING``.
- The original ``ON CONFLICT (metric_date, COALESCE(tool_slug,
  '<site-wide>'))`` in the upsert_analytics_daily function MUST match
  the index expression byte-for-byte. We now document this explicitly
  in the migration header.
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"

EXPECTED_FILES = [
    "001_schema_verification.sql",
    "002_missing_constraints_and_indexes.sql",
    "003_retry_queue_idempotency.sql",
    "004_admin_analytics_support.sql",
    "005_rls_and_least_privilege.sql",
]

pglast = pytest.importorskip("pglast")


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into individual statements, stripping comments.

    PostgreSQL's parser is single-statement (libpg_query parses one
    statement at a time). We split on ';' boundaries that are NOT
    inside a string literal, dollar-quoted body, or comment.

    Handles:
    - ``--`` line comments (until end of line)
    - ``/* ... */`` block comments (can span lines)
    - ``$$ ... $$`` and ``$tag$ ... $tag$`` dollar-quoted bodies
    - ``'...`` single-quoted string literals
    """
    statements = []
    buf = []
    i = 0
    n = len(sql)
    in_dollar = False
    dollar_tag = None
    in_string = False
    in_line_comment = False
    in_block_comment = 0

    while i < n:
        ch = sql[i]

        # ── Comment handling (highest priority) ──
        if in_line_comment:
            buf.append(ch)
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue
        if in_block_comment > 0:
            buf.append(ch)
            if ch == '*' and i + 1 < n and sql[i + 1] == '/':
                buf.append('/')
                i += 2
                in_block_comment -= 1
                continue
            if ch == '/' and i + 1 < n and sql[i + 1] == '*':
                buf.append('*')
                i += 2
                in_block_comment += 1
                continue
            i += 1
            continue

        # ── String literal handling ──
        if in_string:
            buf.append(ch)
            if ch == "'":
                # Escaped quote ('') stays in string
                if i + 1 < n and sql[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                in_string = False
            i += 1
            continue

        # ── Not in any special mode: check for new entries ──
        if not in_dollar and not in_string:
            # Line comment
            if ch == '-' and i + 1 < n and sql[i + 1] == '-':
                in_line_comment = True
                buf.append(ch)
                buf.append('-')
                i += 2
                continue
            # Block comment
            if ch == '/' and i + 1 < n and sql[i + 1] == '*':
                in_block_comment += 1
                buf.append(ch)
                buf.append('*')
                i += 2
                continue
            # String literal
            if ch == "'":
                in_string = True
                buf.append(ch)
                i += 1
                continue
            # Dollar-quoting
            if ch == '$':
                m = re.match(r'\$[A-Za-z_0-9]*\$', sql[i:])
                if m:
                    dollar_tag = m.group(0)
                    in_dollar = True
                    buf.append(dollar_tag)
                    i += len(dollar_tag)
                    continue
        elif in_dollar:
            if ch == '$':
                m = re.match(r'\$[A-Za-z_0-9]*\$', sql[i:])
                if m and m.group(0) == dollar_tag:
                    in_dollar = False
                    dollar_tag = None
                    buf.append(m.group(0))
                    i += len(m.group(0))
                    continue

        # ── Statement terminator ──
        if not in_dollar and not in_string and not in_block_comment and ch == ';':
            stmt = ''.join(buf).strip()
            if stmt:
                # Strip psql meta-commands like \set
                if not stmt.startswith('\\'):
                    statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    stmt = ''.join(buf).strip()
    if stmt and not stmt.startswith('\\'):
        statements.append(stmt)
    return statements


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_migration_parses_with_pglast(name):
    """Every statement in every migration must parse with the real
    PostgreSQL parser (libpg_query)."""
    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    statements = _split_statements(sql)
    assert statements, f"{name} produced no statements; check the splitter."

    errors = []
    for i, stmt in enumerate(statements, 1):
        # Strip trailing comments and whitespace.
        stmt_clean = stmt.strip()
        if not stmt_clean or stmt_clean.startswith('--'):
            continue
        try:
            pglast.parse_sql(stmt_clean)
        except Exception as exc:
            errors.append((i, stmt_clean[:120], str(exc)))

    if errors:
        msg = f"\n{name}: {len(errors)} statement(s) failed to parse:\n"
        for i, snippet, err in errors:
            msg += f"  [{i}] {snippet!r}\n        → {err}\n"
        pytest.fail(msg)


def test_claim_delivery_queue_uses_return_query():
    """The fix for the silent RETURNING bug.

    A bare ``UPDATE ... RETURNING`` inside a ``RETURNS TABLE`` function
    body compiles but does NOT return rows to the caller. The correct
    PL/pgSQL syntax is ``RETURN QUERY UPDATE ... RETURNING``.
    """
    sql = (MIGRATIONS_DIR / "003_retry_queue_idempotency.sql").read_text()
    # Find the function body.
    assert "RETURN QUERY" in sql, (
        "private.claim_delivery_queue must use RETURN QUERY UPDATE ... RETURNING "
        "to actually return rows to the caller. A bare UPDATE ... RETURNING "
        "inside a RETURNS TABLE function silently discards the rows."
    )
    # The RETURN QUERY must wrap an UPDATE that has a RETURNING clause.
    pattern = r"RETURN\s+QUERY\s+UPDATE\s+private\.delivery_queue.*?RETURNING"
    assert re.search(pattern, sql, re.IGNORECASE | re.DOTALL), (
        "Expected `RETURN QUERY UPDATE private.delivery_queue ... RETURNING` "
        "in 003_retry_queue_idempotency.sql."
    )


def test_analytics_daily_on_conflict_matches_index_expression():
    """The ``ON CONFLICT`` clause in upsert_analytics_daily must match
    the unique index expression.

    PostgreSQL requires that the ON CONFLICT conflict_target match an
    existing unique index expression. The match is performed after
    normalizing the expression tree, so schema qualification and
    whitespace differences are tolerated — but the column name, the
    function (COALESCE), and the sentinel literal MUST be the same.

    This test verifies the match by:
    1. Extracting the column name + function name + literal from both
       the index and the ON CONFLICT clause.
    2. Comparing them structurally (not byte-for-byte).

    The full end-to-end verification (applying the migration against a
    real disposable PostgreSQL and exercising the upsert) is done in
    tests/test_migrations_e2e.py.
    """
    idx_sql = (MIGRATIONS_DIR / "002_missing_constraints_and_indexes.sql").read_text()
    fn_sql = (MIGRATIONS_DIR / "004_admin_analytics_support.sql").read_text()
    # Extract the index expression.
    idx_match = re.search(
        r"CREATE\s+UNIQUE\s+INDEX\s+\w+\s+ON\s+public\.analytics_daily\s*\(([^)]+)\)",
        idx_sql, re.IGNORECASE | re.DOTALL
    )
    assert idx_match, "Unique index on analytics_daily not found in migration 002."
    idx_expr = idx_match.group(1).strip()
    # The ON CONFLICT clause should reference the same expression.
    conflict_match = re.search(
        r"ON\s+CONFLICT\s*\(([^)]+)\)", fn_sql, re.IGNORECASE | re.DOTALL
    )
    assert conflict_match, "ON CONFLICT clause not found in upsert_analytics_daily."
    conflict_expr = conflict_match.group(1).strip()

    # Structural comparison: extract the COALESCE column name and the
    # sentinel literal. These MUST match — PG rejects otherwise.
    def _extract_signature(expr: str) -> tuple:
        # Matches: COALESCE(<maybe-schema-qualified>.tool_slug, '<sentinel>')
        # Capture the column name and the sentinel literal.
        m = re.search(
            r"COALESCE\s*\(\s*(?:\w+\.)*\.?(\w+)\s*,\s*'([^']+)'\s*\)",
            expr, re.IGNORECASE
        )
        if not m:
            return (None, None)
        return (m.group(1).lower(), m.group(2))

    idx_sig = _extract_signature(idx_expr)
    conflict_sig = _extract_signature(conflict_expr)
    assert idx_sig == conflict_sig, (
        f"Index expression and ON CONFLICT expression must reference the "
        f"same column and sentinel literal.\n"
        f"  index signature:   {idx_sig}\n"
        f"  conflict signature: {conflict_sig}"
    )
    # The first column (metric_date) must also match.
    idx_first = idx_expr.split(",")[0].strip().split(".")[-1].lower()
    conflict_first = conflict_expr.split(",")[0].strip().split(".")[-1].lower()
    assert idx_first == conflict_first, (
        f"First column must match: index={idx_first}, conflict={conflict_first}"
    )


def test_migrations_use_idempotent_blocks():
    """Every CREATE INDEX / CREATE TABLE in migrations 002, 003, 004
    must be either:

    (a) Wrapped in a DO block that performs an IF NOT EXISTS check
        before executing the CREATE, OR
    (b) Use the ``CREATE ... IF NOT EXISTS`` form directly.

    Bare ``CREATE INDEX`` outside a DO block is rejected because it
    fails on re-run when the index already exists.

    Detection strategy:
    - Strip dollar-quoted bodies (function definitions with AS $$ ... $$).
    - Strip line and block comments.
    - For each remaining CREATE INDEX / CREATE TABLE, check that the
      preceding 200 characters contain "IF NOT EXISTS" or that the
      CREATE itself uses "IF NOT EXISTS".
    """
    for name in ("002_missing_constraints_and_indexes.sql",
                 "003_retry_queue_idempotency.sql",
                 "004_admin_analytics_support.sql"):
        sql = (MIGRATIONS_DIR / name).read_text()
        # Strip dollar-quoted function bodies (AS $$ ... $$). Keep
        # DO $$ ... $$ blocks because they are the guard wrapper.
        # We strip the AS $$...$$ form (with AS keyword before $$).
        without_func_bodies = re.sub(
            r'\bAS\s+\$[A-Za-z_0-9]*\$.*?\$[A-Za-z_0-9]*\$',
            ' /* function_body */ ',
            sql, flags=re.DOTALL | re.IGNORECASE
        )
        # Strip line and block comments.
        without_comments = re.sub(r'--[^\n]*', '', without_func_bodies)
        without_comments = re.sub(r'/\*.*?\*/', ' ', without_comments, flags=re.DOTALL)

        # Find every CREATE INDEX/TABLE statement.
        unguarded = []
        for m in re.finditer(
            r"CREATE\s+(?:UNIQUE\s+)?(?:INDEX|TABLE)\s+(?!IF\s+NOT\s+EXISTS)",
            without_comments, re.IGNORECASE
        ):
            # Look at the 400 chars BEFORE the CREATE to see if it's
            # inside an IF (NOT) EXISTS (...) THEN block, or inside
            # an EXECUTE $stmt$ ... $stmt$ block (which is itself
            # inside a DO $$ ... IF NOT EXISTS THEN ... $$ block).
            preceding = without_comments[max(0, m.start() - 400):m.start()]
            # Find the closest preceding "THEN" — the CREATE is
            # guarded if there's an IF NOT EXISTS check before that THEN.
            last_then = preceding.rfind("THEN")
            if last_then > 0:
                # Look for "IF NOT EXISTS" or "AND NOT EXISTS" before THEN.
                chunk_before_then = preceding[:last_then]
                if ("NOT EXISTS" in chunk_before_then or
                    "IF NOT EXISTS" in chunk_before_then):
                    continue  # Guarded.
            # Also accept the case where the CREATE is inside an
            # EXECUTE $stmt$ ... $stmt$ block (used when the CREATE
            # uses an expression that can't appear in a regular CREATE
            # INDEX IF NOT EXISTS — see migration 002's analytics_daily
            # unique index on COALESCE(...)).
            if "EXECUTE $stmt$" in preceding or 'EXECUTE $stmt$' in preceding:
                continue
            unguarded.append(m.group(0))

        assert not unguarded, (
            f"{name}: bare CREATE statement(s) found outside a DO-block "
            f"guard. Every CREATE must use IF NOT EXISTS directly or be "
            f"wrapped in a DO $$ ... IF NOT EXISTS (...) THEN ... END $$ "
            f"block so the migration is safe to re-run. Found: {unguarded}"
        )
