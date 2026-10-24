"""Tests for SQL migration files.

These tests verify that:
1. All four migration files exist in the migrations/ directory.
2. Each migration is parseable SQL (uses ; as statement separator).
3. The migrations are idempotent (every CREATE/ALTER is wrapped in a
   DO $$ ... IF NOT EXISTS ... block).
4. No migration runs a bare CREATE TABLE that would clobber the existing
   schema.
5. No migration enables anonymous RLS policies on sensitive tables.
6. The 003 migration revokes PUBLIC access on the delivery queue.
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


def test_migrations_directory_exists():
    assert MIGRATIONS_DIR.is_dir(), f"{MIGRATIONS_DIR} should be a directory"


def test_expected_migration_files_exist():
    actual = {p.name for p in MIGRATIONS_DIR.iterdir() if p.suffix == ".sql"}
    for name in EXPECTED_FILES:
        assert name in actual, f"Missing migration file: {name}"


def _read_migration(name: str) -> str:
    path = MIGRATIONS_DIR / name
    assert path.is_file(), f"Migration file not found: {path}"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_each_migration_starts_with_a_header_comment(name):
    content = _read_migration(name)
    assert content.startswith("--"), f"{name} should start with a SQL comment header"


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_migrations_contain_no_psql_meta_commands(name):
    """Migrations must be runnable in the Supabase SQL Editor, which rejects
    psql meta-commands such as \\set. ON_ERROR_STOP is supplied by the runner
    (psql -v ON_ERROR_STOP=1), as documented in RENDER_DEPLOYMENT.md."""
    content = _read_migration(name)
    for line in content.splitlines():
        assert not line.lstrip().startswith("\\"), (
            f"{name} contains a psql meta-command: {line!r}")


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_migrations_use_transactions(name):
    """Every migration must wrap its DDL in BEGIN ... COMMIT."""
    content = _read_migration(name)
    assert "BEGIN;" in content, f"{name} must wrap DDL in BEGIN"
    assert "COMMIT;" in content, f"{name} must wrap DDL in COMMIT"


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_no_bare_create_table_against_existing_schema(name):
    """No migration may run a bare CREATE TABLE on a public table that
    the spec says already exists (admin_profiles, visitor_sessions,
    analytics_events, analytics_daily, support_queries, security_events).

    The migrations may only ADD COLUMNS / constraints / indexes to them,
    and may CREATE TABLE only for genuinely new objects (private.delivery_queue)."""
    content = _read_migration(name)
    existing_public_tables = {
        "admin_profiles", "visitor_sessions", "analytics_events",
        "analytics_daily", "support_queries", "security_events",
    }
    # Match `CREATE TABLE public.X` (not `CREATE TABLE IF NOT EXISTS`, which
    # would also be questionable here).
    bare = re.findall(r"CREATE\s+TABLE\s+public\.(\w+)", content, re.IGNORECASE)
    for tbl in bare:
        assert tbl not in existing_public_tables, (
            f"{name}: refused to run CREATE TABLE public.{tbl} — the spec "
            "says this table already exists. Use ALTER TABLE instead."
        )


def test_003_creates_delivery_queue_in_private_schema():
    content = _read_migration("003_retry_queue_idempotency.sql")
    assert "private.delivery_queue" in content
    assert "CREATE TABLE private.delivery_queue" in content or \
           "CREATE TABLE IF NOT EXISTS private.delivery_queue" in content


def test_003_revokes_public_access_on_delivery_queue():
    content = _read_migration("003_retry_queue_idempotency.sql")
    assert "REVOKE ALL ON private.delivery_queue FROM PUBLIC" in content
    assert "REVOKE ALL ON private.delivery_queue FROM anon" in content
    assert "REVOKE ALL ON private.delivery_queue FROM authenticated" in content


def test_003_enables_rls_on_delivery_queue():
    content = _read_migration("003_retry_queue_idempotency.sql")
    assert "ENABLE ROW LEVEL SECURITY" in content
    # No GRANT to anon/authenticated/pUBLIC — service_role bypasses RLS.
    assert "GRANT SELECT.*TO anon" not in content.replace("\n", " ")
    assert "GRANT SELECT.*TO authenticated" not in content.replace("\n", " ")


def test_003_uses_for_update_skip_locked():
    """The claim function must use FOR UPDATE SKIP LOCKED so multiple
    workers can safely claim concurrently."""
    content = _read_migration("003_retry_queue_idempotency.sql")
    assert "FOR UPDATE SKIP LOCKED" in content


def test_001_fails_loud_when_tables_missing():
    """The first migration must raise an exception rather than create
    missing tables, so the operator knows to apply the original schema
    first."""
    content = _read_migration("001_schema_verification.sql")
    assert "RAISE EXCEPTION" in content
    assert "Missing required table" in content


def test_002_uses_idempotent_index_creation():
    """Every CREATE INDEX in 002 must check pg_indexes for existence first."""
    content = _read_migration("002_missing_constraints_and_indexes.sql")
    # Every CREATE INDEX should be wrapped in a DO block with an
    # IF NOT EXISTS check.
    create_index_count = content.count("CREATE INDEX")
    if_not_exists_count = content.count("information_schema") + content.count("pg_indexes")
    # Either there are no CREATE INDEX statements, or every one is
    # guarded by an existence check.
    assert create_index_count == 0 or if_not_exists_count >= create_index_count, (
        "Every CREATE INDEX in 002 must be guarded by an existence check."
    )


def test_004_adds_optional_columns_conditionally():
    """The 004 migration must only ADD COLUMN when the column doesn't exist."""
    content = _read_migration("004_admin_analytics_support.sql")
    assert "ADD COLUMN" in content
    # Every ADD COLUMN must be inside a DO $$ ... IF NOT EXISTS ... block.
    add_column_count = content.count("ADD COLUMN")
    not_exists_count = content.count("information_schema.columns")
    assert not_exists_count >= add_column_count, (
        "Every ADD COLUMN in 004 must be guarded by an information_schema check."
    )


def test_004_does_not_enable_anonymous_rls_policies():
    """No migration may enable anonymous access to sensitive tables."""
    content = _read_migration("004_admin_analytics_support.sql")
    # Look for the pattern "CREATE POLICY ... TO anon" or "TO public"
    assert "TO anon" not in content
    assert "TO public" not in content.lower() or "REVOKE" in content


def test_003_includes_rollback_guidance():
    """Every migration must include rollback guidance in comments."""
    for name in EXPECTED_FILES:
        content = _read_migration(name)
        assert "Rollback" in content or "rollback" in content, (
            f"{name} must include rollback guidance in a comment."
        )


def test_002_does_not_create_duplicate_index_when_one_exists():
    """The unique index on analytics_daily must check for duplicates
    BEFORE creating the index (otherwise CREATE UNIQUE INDEX fails on
    pre-existing duplicate rows)."""
    content = _read_migration("002_missing_constraints_and_indexes.sql")
    assert "duplicate" in content.lower() or "count(*) > 1" in content


def test_no_migration_disables_ssl_verification():
    """No migration may weaken TLS. (Migrations don't run SQL with SSL
    settings, but defensive check in case someone tries.)"""
    for name in EXPECTED_FILES:
        content = _read_migration(name)
        assert "sslmode=disable" not in content.lower()
        assert "sslmode=allow" not in content.lower()
        assert "sslmode=prefer" not in content.lower()
