"""Behavioral access-control tests against a REAL PostgreSQL instance.

Structural checks (grep the SQL) cannot show that RLS actually filters rows.
These tests apply migrations 000-005 to a disposable database, then switch to
the ``anon`` / ``authenticated`` / ``service_role`` roles with ``SET ROLE`` and
verify what each role can really do.

They skip when no disposable server is reachable (same contract as
test_migrations_e2e.py). Set PG_PORT to point at a non-default port.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"
PG_PORT = int(os.environ.get("PG_PORT", "55432"))
PG_INSTALL_DIR = Path(os.environ.get("PG_INSTALL_DIR", "/tmp/pg-install"))
PG_BIN = PG_INSTALL_DIR / "bin"
DB = "anvil_rls_test"
ADMIN_UID = "11111111-1111-1111-1111-111111111111"
USER_UID = "22222222-2222-2222-2222-222222222222"


def _psql_path():
    if (PG_BIN / "psql").exists():
        return str(PG_BIN / "psql")
    return shutil.which("psql")


def _run(sql, *, db=DB, file=None):
    cmd = [_psql_path(), "-h", "/var/run/postgresql", "-p", str(PG_PORT),
           "-U", "postgres", "-d", db, "-v", "ON_ERROR_STOP=1", "-tA"]
    if file:
        cmd += ["-f", str(file)]
    else:
        cmd += ["-c", sql]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def _server_up():
    p = _psql_path()
    if not p:
        return False
    r = subprocess.run([p, "-h", "/var/run/postgresql", "-p", str(PG_PORT),
                        "-U", "postgres", "-d", "postgres", "-tA", "-c", "SELECT 1"],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(
    not (_psql_path() and _server_up()),
    reason="disposable PostgreSQL not reachable (set PG_PORT / start the server)",
)


@pytest.fixture(scope="module")
def db():
    _run("DROP DATABASE IF EXISTS %s;" % DB, db="postgres")
    assert _run("CREATE DATABASE %s;" % DB, db="postgres").returncode == 0
    assert _run("", file=MIGRATIONS / "000_base_schema.sql").returncode == 0
    for name in ["001_schema_verification.sql",
                 "002_missing_constraints_and_indexes.sql",
                 "003_retry_queue_idempotency.sql",
                 "004_admin_analytics_support.sql",
                 "005_rls_and_least_privilege.sql"]:
        r = _run("", file=MIGRATIONS / name)
        assert r.returncode == 0, f"{name} failed: {r.stderr}"
    # Seed: one admin, one support row. Superuser bypasses RLS for seeding.
    seed = f"""
        INSERT INTO public.admin_profiles (user_id, display_name)
            VALUES ('{ADMIN_UID}', 'Admin');
        INSERT INTO public.support_queries (name, email, category, subject, message, status)
            VALUES ('Visitor', 'v@example.com', 'bug_report', 'Hi', 'Body text', 'new');
    """
    assert _run(seed).returncode == 0
    yield DB


def _as(role, uid, sql):
    """Run ``sql`` as ``role`` with the given JWT subject, return the result."""
    # DO blocks print nothing, so the output contains only the query result.
    claim = (f"DO $$ BEGIN PERFORM set_config('request.jwt.claim.sub', '{uid}', false); END $$;"
             if uid else "")
    script = f"SET ROLE {role}; {claim} {sql};"
    return _run(script)


def _denied(result):
    return result.returncode != 0 and "permission denied" in result.stderr.lower()


class TestAnonymousRole:
    def test_anon_cannot_read_support_queries(self, db):
        r = _as("anon", None, "SELECT count(*) FROM public.support_queries")
        assert _denied(r), r.stderr

    def test_anon_cannot_read_audit_log(self, db):
        r = _as("anon", None, "SELECT count(*) FROM private.audit_log")
        assert _denied(r), r.stderr

    def test_anon_cannot_execute_analytics_upsert(self, db):
        r = _as("anon", None,
                "SELECT public.upsert_analytics_daily(current_date, NULL, 1, 1, 1, 0)")
        assert _denied(r), r.stderr


class TestAuthenticatedRole:
    def test_non_admin_sees_no_support_rows(self, db):
        """RLS must filter, not error: a signed-in non-admin gets zero rows."""
        r = _as("authenticated", USER_UID,
                "SELECT count(*) FROM public.support_queries")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1].strip() == "0"

    def test_admin_sees_support_rows(self, db):
        r = _as("authenticated", ADMIN_UID,
                "SELECT count(*) FROM public.support_queries")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1].strip() == "1"

    def test_non_admin_cannot_update_support_status(self, db):
        r = _as("authenticated", USER_UID,
                "UPDATE public.support_queries SET status = 'closed'")
        # RLS filters the row set to nothing, so zero rows are touched.
        assert r.returncode == 0, r.stderr
        check = _run("SELECT count(*) FROM public.support_queries WHERE status='closed'")
        assert check.stdout.strip() == "0"

    def test_authenticated_cannot_change_identity_columns(self, db):
        """Column-level grant: name/email/message are not editable by any user."""
        r = _as("authenticated", ADMIN_UID,
                "UPDATE public.support_queries SET message = 'tampered'")
        assert _denied(r), r.stderr

    def test_authenticated_cannot_read_audit_log(self, db):
        r = _as("authenticated", ADMIN_UID, "SELECT count(*) FROM private.audit_log")
        assert _denied(r), r.stderr

    def test_authenticated_cannot_execute_analytics_upsert(self, db):
        """Regression: previously granted to authenticated, so any signed-in
        user could write analytics rows through PostgREST RPC."""
        r = _as("authenticated", USER_UID,
                "SELECT public.upsert_analytics_daily(current_date, NULL, 1, 1, 1, 0)")
        assert _denied(r), r.stderr

    def test_admin_check_function_still_callable_by_authenticated(self, db):
        """RLS policies depend on private.is_admin(); it must stay executable."""
        r = _as("authenticated", ADMIN_UID, "SELECT private.is_admin()")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip().splitlines()[-1].strip() == "t"


class TestServiceRole:
    def test_service_role_can_execute_analytics_upsert(self, db):
        r = _as("service_role", None,
                "SELECT public.upsert_analytics_daily(current_date, NULL, 5, 2, 1, 0)")
        assert r.returncode == 0, r.stderr

    def test_rls_enabled_on_all_application_tables(self, db):
        tables = ["admin_profiles", "visitor_sessions", "analytics_events",
                  "analytics_daily", "support_queries", "security_events"]
        r = _run(
            "SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity"
        )
        enabled = set(r.stdout.strip().split(","))
        assert set(tables) <= enabled, f"RLS missing on: {set(tables) - enabled}"
