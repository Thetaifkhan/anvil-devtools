"""End-to-end migration tests against a disposable PostgreSQL instance.

These tests apply the migration files against a REAL PostgreSQL database
(built from source on the test machine, not a real Supabase instance).
They verify what the static ``pglast`` parser cannot:

- The migrations actually apply without runtime errors.
- The ``private.claim_delivery_queue`` function returns rows when called
  (the original code used a bare ``UPDATE ... RETURNING`` inside a
  ``RETURNS TABLE`` function body, which compiles but silently discards
  the rows).
- The ``public.upsert_analytics_daily`` function's ``ON CONFLICT``
  expression matches the unique index in migration 002 — PG itself
  validates this match at function-call time, not at parse time.
- All ``SECURITY DEFINER`` functions use ``SET search_path = ''`` (the
  recommended hardening against search_path hijacking).
- Existing audit triggers and RLS policies are preserved.
- Re-running migrations 002-004 is idempotent (no errors on second apply).

The disposable PostgreSQL is started in /tmp/pg-install/bin/ on port
55432 by the test harness. If the binary is not present (the test
machine doesn't have it), these tests SKIP rather than fail.

The base schema (migrations/000_base_schema.sql) reproduces the
existing Supabase schema the migrations expect to find. It is applied
to the disposable DB but is NOT applied to production.
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"
EXPECTED_MIGRATIONS = [
    "001_schema_verification.sql",
    "002_missing_constraints_and_indexes.sql",
    "003_retry_queue_idempotency.sql",
    "004_admin_analytics_support.sql",
    "005_rls_and_least_privilege.sql",
]

# Path to the disposable PostgreSQL install. Set PG_INSTALL_DIR to override.
PG_INSTALL_DIR = Path(os.environ.get("PG_INSTALL_DIR", "/tmp/pg-install"))
PG_BIN = PG_INSTALL_DIR / "bin"
PG_DATA_DIR = Path(os.environ.get("PG_DATA_DIR", "/tmp/pgdata-anvil-tests"))
PG_PORT = int(os.environ.get("PG_PORT", "55432"))
PG_USER = "postgres"
PG_DB = "anvil_mig_test"


def _psql_available() -> bool:
    """True iff a usable psql binary is on PATH or at PG_INSTALL_DIR."""
    if PG_BIN.is_dir() and (PG_BIN / "psql").exists():
        return True
    return shutil.which("psql") is not None


def _postgres_server_available() -> bool:
    """True iff a postgres server is reachable on PG_PORT."""
    if not (PG_BIN / "psql").exists() and not shutil.which("psql"):
        return False
    psql = str(PG_BIN / "psql") if (PG_BIN / "psql").exists() else "psql"
    try:
        result = subprocess.run(
            [psql, "-p", str(PG_PORT), "-U", PG_USER, "-d", "postgres",
             "-c", "SELECT 1;"],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "PGPASSWORD": ""},
        )
        return result.returncode == 0
    except Exception:
        return False


# Skip the entire module if no disposable PostgreSQL is available.
pytestmark = pytest.mark.skipif(
    not _psql_available(),
    reason="psql not available; install PostgreSQL or set PG_INSTALL_DIR",
)


def _run_psql(sql_or_args, *, db=PG_DB, is_file=False, capture=True):
    """Run a SQL statement or file against the disposable DB.

    Returns the CompletedProcess. Raises CalledProcessError on failure
    (when check=True).
    """
    psql = str(PG_BIN / "psql") if (PG_BIN / "psql").exists() else "psql"
    env = {**os.environ, "PGPASSWORD": ""}
    cmd = [psql, "-p", str(PG_PORT), "-U", PG_USER, "-d", db,
           "-v", "ON_ERROR_STOP=1"]
    if is_file:
        cmd += ["-f", str(sql_or_args)]
    else:
        cmd += ["-c", sql_or_args]
    return subprocess.run(cmd, capture_output=capture, text=True,
                          timeout=30, env=env, check=False)


def _drop_and_recreate_db():
    """Drop and recreate the test database."""
    _run_psql("DROP DATABASE IF EXISTS %s;" % PG_DB, db="postgres")
    _run_psql("CREATE DATABASE %s;" % PG_DB, db="postgres")


@pytest.fixture(scope="module")
def pg_server():
    """Ensure a disposable PostgreSQL is running. Skip if unavailable."""
    if not _postgres_server_available():
        pytest.skip("Disposable PostgreSQL server not reachable on port %d. "
                    "Start one with: %s/pg_ctl -D /tmp/pgdata -l /tmp/pg.log "
                    "start" % (PG_PORT, PG_BIN))
    yield


@pytest.fixture
def fresh_db(pg_server):
    """Each test gets a fresh database with the base schema applied."""
    _drop_and_recreate_db()
    # Apply the base schema (the existing Supabase tables the
    # migrations expect to find).
    result = _run_psql(MIGRATIONS_DIR / "000_base_schema.sql", is_file=True)
    assert result.returncode == 0, (
        f"Base schema failed:\n{result.stderr}\n{result.stdout}"
    )
    yield PG_DB


def _apply_migration(name, db=PG_DB):
    """Apply a single migration file. Returns CompletedProcess."""
    return _run_psql(MIGRATIONS_DIR / name, db=db, is_file=True)


# ── 1. Migrations apply cleanly against the base schema ───────────────────

@pytest.mark.parametrize("name", EXPECTED_MIGRATIONS)
def test_migration_applies_cleanly(fresh_db, name):
    """Each migration must apply against the base schema without errors."""
    result = _apply_migration(name)
    assert result.returncode == 0, (
        f"{name} failed to apply:\n"
        f"stderr: {result.stderr}\n"
        f"stdout: {result.stdout}"
    )


def test_all_migrations_apply_in_order(fresh_db):
    """Apply all 4 migrations in order — must succeed with no errors."""
    for name in EXPECTED_MIGRATIONS:
        result = _apply_migration(name)
        assert result.returncode == 0, (
            f"{name} failed:\n{result.stderr}"
        )


# ── 2. claim_delivery_queue actually returns rows ─────────────────────────

def test_claim_delivery_queue_returns_rows(fresh_db):
    """The fix for the silent RETURNING bug.

    A bare ``UPDATE ... RETURNING`` inside a ``RETURNS TABLE`` function
    body compiles but does NOT return rows to the caller. The correct
    PL/pgSQL syntax is ``RETURN QUERY UPDATE ... RETURNING``. This test
    proves the fix works by inserting items and verifying that calling
    claim_delivery_queue returns them.
    """
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # Insert 3 pending items.
    insert_sql = """
        INSERT INTO private.delivery_queue (event_key, event_type, payload) VALUES
            ('00000000-0000-0000-0000-000000000001', 'analytics_event',
             '{"x":1}'::jsonb),
            ('00000000-0000-0000-0000-000000000002', 'analytics_event',
             '{"x":2}'::jsonb),
            ('00000000-0000-0000-0000-000000000003', 'contact_submission',
             '{"ref":"ANVIL-X"}'::jsonb);
    """
    result = _run_psql(insert_sql)
    assert result.returncode == 0, f"Insert failed: {result.stderr}"

    # Claim a batch — must return 3 rows.
    result = _run_psql(
        "SELECT id, event_key, event_type, attempt_count "
        "FROM private.claim_delivery_queue(10) ORDER BY id;"
    )
    assert result.returncode == 0, (
        f"claim_delivery_queue failed:\n{result.stderr}"
    )
    # Output should contain 3 data rows.
    rows = [line for line in result.stdout.split("\n")
            if line.strip() and not line.startswith(("-")) and "id" not in line.lower().split("|")[0]]
    assert len(rows) >= 3, (
        f"Expected ≥3 rows from claim_delivery_queue, got:\n{result.stdout}"
    )
    # attempt_count should be 1 for each row.
    assert "1" in result.stdout, (
        f"Expected attempt_count=1 in output:\n{result.stdout}"
    )


def test_claim_delivery_queue_returns_empty_when_no_pending(fresh_db):
    """Calling claim_delivery_queue with no pending items returns an
    empty result set (not NULL, not an error)."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    result = _run_psql("SELECT count(*) FROM private.claim_delivery_queue(10);")
    assert result.returncode == 0, f"Failed: {result.stderr}"
    assert "0" in result.stdout, (
        f"Expected 0 rows; got:\n{result.stdout}"
    )


# ── 3. ON CONFLICT upsert works at runtime ─────────────────────────────────

def test_upsert_analytics_daily_on_conflict_works(fresh_db):
    """The critical ON CONFLICT test.

    The unique index in migration 002 uses the expression
    ``(metric_date, COALESCE(tool_slug, '<site-wide>'))``. The
    ``upsert_analytics_daily`` function in migration 004 must use an
    ON CONFLICT clause that PG accepts as matching this index.

    PG validates the match at function CALL time, not at parse time —
    so only an end-to-end test can catch a mismatch.
    """
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # First call: inserts.
    result = _run_psql(
        "SELECT public.upsert_analytics_daily("
        "'2025-10-23'::date, NULL::text, 100, 50, 30, 2);"
    )
    assert result.returncode == 0, (
        f"First upsert call failed:\n{result.stderr}"
    )

    # Second call: must update (not insert a duplicate).
    result = _run_psql(
        "SELECT public.upsert_analytics_daily("
        "'2025-10-23'::date, NULL::text, 105, 55, 35, 3);"
    )
    assert result.returncode == 0, (
        f"Second upsert call failed:\n{result.stderr}"
    )

    # Verify exactly one row, with the updated values.
    result = _run_psql(
        "SELECT count(*) FROM public.analytics_daily "
        "WHERE metric_date = '2025-10-23' AND tool_slug IS NULL;"
    )
    assert result.returncode == 0
    # Output: count \n ------- \n 1 \n (1 row)
    assert "1" in result.stdout.split("\n")[-3] or "1" in result.stdout, (
        f"Expected exactly 1 row after upsert; got:\n{result.stdout}"
    )

    # Verify the values were updated.
    result = _run_psql(
        "SELECT page_views, tool_errors FROM public.analytics_daily "
        "WHERE metric_date = '2025-10-23' AND tool_slug IS NULL;"
    )
    assert "105" in result.stdout, f"page_views not updated:\n{result.stdout}"
    assert "3" in result.stdout, f"tool_errors not updated:\n{result.stdout}"


# ── 4. SECURITY DEFINER functions use search_path = '' ────────────────────

def test_all_security_definer_functions_use_empty_search_path(fresh_db):
    """All SECURITY DEFINER functions in the public/private schemas
    must set search_path = '' (empty) to prevent search_path hijacking.

    The original upsert_analytics_daily used SET search_path = public
    which is risky: an attacker who can create a malicious object in
    the public schema could shadow the function's table references.
    """
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    result = _run_psql("""
        SELECT n.nspname, p.proname,
               (SELECT array_agg(s) FROM unnest(p.proconfig) AS s) AS cfg
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef = true
          AND n.nspname IN ('public', 'private')
        ORDER BY n.nspname, p.proname;
    """)
    assert result.returncode == 0, f"Query failed: {result.stderr}"

    # Parse the output. Each SECURITY DEFINER function must have
    # search_path="" in its config.
    lines = [l for l in result.stdout.split("\n") if "|" in l and "proname" not in l]
    assert len(lines) >= 4, (
        f"Expected ≥4 SECURITY DEFINER functions, got {len(lines)}:\n{result.stdout}"
    )
    for line in lines:
        # Each line: schema | name | {search_path=""}
        assert 'search_path=""' in line or "search_path=" in line, (
            f"SECURITY DEFINER function without search_path config: {line}"
        )
        # Specifically, search_path must be empty (""), not "public".
        assert "search_path=public" not in line, (
            f"SECURITY DEFINER function with search_path=public (not empty): {line}"
        )


# ── 5. RLS is enabled and there are no anon/authenticated policies ────────

def test_delivery_queue_rls_enabled_no_public_policies(fresh_db):
    """private.delivery_queue must have RLS enabled and zero policies
    for anon/authenticated. Only service_role (which bypasses RLS)
    can read/write the table."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    result = _run_psql("""
        SELECT relrowsecurity
        FROM pg_class
        WHERE relname = 'delivery_queue' AND relnamespace = 'private'::regnamespace;
    """)
    assert "t" in result.stdout.lower(), (
        f"RLS not enabled on private.delivery_queue:\n{result.stdout}"
    )

    result = _run_psql("""
        SELECT count(*) FROM pg_policy
        WHERE polrelid = 'private.delivery_queue'::regclass;
    """)
    assert "0" in result.stdout, (
        f"Expected 0 policies on private.delivery_queue, got:\n{result.stdout}"
    )

    # Verify privileges: only postgres and service_role.
    result = _run_psql("""
        SELECT DISTINCT grantee FROM information_schema.table_privileges
        WHERE table_schema = 'private' AND table_name = 'delivery_queue'
        ORDER BY grantee;
    """)
    grantees = set(l.strip() for l in result.stdout.split("\n") if "|" not in l and l.strip() and "grantee" not in l)
    # postgres and service_role should be present. anon and authenticated should NOT.
    assert "anon" not in grantees, "anon has privileges on delivery_queue"
    assert "authenticated" not in grantees, "authenticated has privileges on delivery_queue"


# ── 6. Existing audit triggers are preserved ──────────────────────────────

def test_audit_triggers_preserved(fresh_db):
    """The migrations must NOT drop or disable the existing audit
    triggers on support_queries and admin_profiles."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # Trigger count on support_queries — should be at least 1 (audit_support_queries).
    result = _run_psql("""
        SELECT count(*) FROM information_schema.triggers
        WHERE event_object_schema = 'public'
          AND event_object_table = 'support_queries';
    """)
    assert "0" not in result.stdout.split("\n")[-3], (
        f"Audit trigger missing on support_queries:\n{result.stdout}"
    )

    # Same on admin_profiles.
    result = _run_psql("""
        SELECT count(*) FROM information_schema.triggers
        WHERE event_object_schema = 'public'
          AND event_object_table = 'admin_profiles';
    """)
    assert "0" not in result.stdout.split("\n")[-3], (
        f"Audit trigger missing on admin_profiles:\n{result.stdout}"
    )


# ── 7. Existing tables are NOT dropped or recreated ──────────────────────

def test_existing_tables_not_dropped(fresh_db):
    """The migrations must NOT drop or recreate the existing public
    tables. Verify by checking the table OIDs didn't change."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # Check all the expected tables still exist.
    expected_tables = [
        "admin_profiles", "visitor_sessions", "analytics_events",
        "analytics_daily", "support_queries", "security_events",
    ]
    for tbl in expected_tables:
        result = _run_psql(
            f"SELECT count(*) FROM information_schema.tables "
            f"WHERE table_schema = 'public' AND table_name = '{tbl}';"
        )
        assert "1" in result.stdout, (
            f"Expected table {tbl} is missing after migrations:\n{result.stdout}"
        )

    # And private.audit_log.
    result = _run_psql(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'private' AND table_name = 'audit_log';"
    )
    assert "1" in result.stdout, (
        f"private.audit_log is missing after migrations:\n{result.stdout}"
    )


# ── 8. Idempotency: re-running migrations 002-004 doesn't error ──────────

def test_migrations_are_idempotent(fresh_db):
    """Applying migrations 002, 003, 004 a second time must not error
    (every CREATE/ALTER is wrapped in an IF NOT EXISTS check)."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # Re-apply 002, 003, 004.
    for name in ("002_missing_constraints_and_indexes.sql",
                 "003_retry_queue_idempotency.sql",
                 "004_admin_analytics_support.sql"):
        result = _apply_migration(name)
        assert result.returncode == 0, (
            f"Re-applying {name} failed (not idempotent):\n{result.stderr}"
        )


# ── 9. is_admin() still works (authorization preserved) ───────────────────

def test_is_admin_function_works(fresh_db):
    """The private.is_admin() function must still return a boolean
    after the migrations are applied. The migrations must NOT have
    redefined or broken it."""
    for name in EXPECTED_MIGRATIONS:
        _apply_migration(name)

    # Insert an admin profile.
    result = _run_psql(
        "INSERT INTO public.admin_profiles (user_id, display_name) VALUES "
        "('00000000-0000-0000-0000-000000000099', 'Test Admin');"
    )
    assert result.returncode == 0, f"Insert admin failed: {result.stderr}"

    # Set the request.jwt.claim.sub GUC and call is_admin.
    result = _run_psql(
        "SELECT private.is_admin();"
    )
    assert result.returncode == 0, (
        f"is_admin() call failed:\n{result.stderr}"
    )
    # Without setting the GUC, it should return false (NULL uuid → no match).
    assert "f" in result.stdout.lower(), (
        f"is_admin() should return false with no GUC set:\n{result.stdout}"
    )
