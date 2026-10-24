"""Production safety tests.

Verifies that:
- The Flask app NEVER applies database migrations automatically.
- The migrations are NOT run by import-time code.
- The Dockerfile does NOT run migrations on container start.
- The render.yaml does NOT run migrations on deploy.
- No credentials are hardcoded.
- RLS / audit triggers are preserved by migrations (verified by
  reading the migration SQL — these are static checks; for live
  verification, the operator runs the migrations against a staging
  DB and checks via the dashboard).
- The Supabase service-role key is never serialized to a template
  or shipped to the browser.
"""
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── 1. No auto-migration ─────────────────────────────────────────────────

def test_flask_app_does_not_apply_migrations_automatically():
    """The Flask app must NEVER run SQL migrations on boot or per-request.

    The operator must apply migrations manually via psql or the
    Supabase SQL Editor (see RENDER_DEPLOYMENT.md).
    """
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    # The app must NOT call any of: cur.execute("CREATE TABLE..."),
    # cur.execute("ALTER TABLE..."), or invoke a migration file.
    forbidden_patterns = [
        r'execute\s*\(\s*"\s*CREATE\s+(?:TABLE|INDEX|UNIQUE)',
        r'execute\s*\(\s*"\s*ALTER\s+TABLE',
        r'execute\s*\(\s*"\s*DROP\s+TABLE',
        r"subprocess\..*psql",
        r"subprocess\..*migration",
        r"runpy\..*migration",
        r"importlib\..*migration",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, app_py, re.IGNORECASE), (
            f"app.py contains a forbidden pattern that looks like auto-migration: "
            f"{pattern}"
        )


def test_dockerfile_does_not_run_migrations():
    """The Dockerfile must NOT run migrations on container build or start.

    The Dockerfile's CMD line runs gunicorn. It must NOT chain a
    ``psql -f migrations/...`` invocation.
    """
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    forbidden = [
        r"psql\s+.*migrations",
        r"migrations/\d+",
        r"python.*migrate",
        r"alembic\s+upgrade",
        r"flask\s+db\s+migrate",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, dockerfile, re.IGNORECASE), (
            f"Dockerfile contains a forbidden pattern that auto-runs migrations: "
            f"{pattern}"
        )


def test_render_yaml_does_not_run_migrations():
    """The render.yaml must NOT auto-run migrations on deploy."""
    render_yaml = (ROOT / "render.yaml").read_text(encoding="utf-8")
    forbidden = [
        r"psql\s+.*migrations",
        r"migrations/\d+",
        r"python.*migrate",
        r"alembic\s+upgrade",
    ]
    for pattern in forbidden:
        assert not re.search(pattern, render_yaml, re.IGNORECASE), (
            f"render.yaml contains a forbidden pattern that auto-runs migrations: "
            f"{pattern}"
        )


def test_no_alembic_or_flask_migrate_dependency():
    """The project must NOT use Alembic or Flask-Migrate, both of which
    auto-apply migrations on boot when misconfigured."""
    for req_file in ("requirements.txt", "requirements-dev.txt"):
        path = ROOT / req_file
        if not path.exists():
            continue
        contents = path.read_text(encoding="utf-8")
        assert "alembic" not in contents.lower(), (
            f"{req_file} contains alembic — auto-migrations could be triggered."
        )
        assert "flask-migrate" not in contents.lower(), (
            f"{req_file} contains flask-migrate — auto-migrations could be triggered."
        )


# ── 2. No hardcoded credentials ───────────────────────────────────────────

def test_no_hardcoded_supabase_credentials():
    """No real Supabase credentials should be hardcoded anywhere in
    the source. The .env.example may contain placeholders only."""
    # Patterns that look like real Supabase keys (eyJ... is the start
    # of a base64-encoded JWT).
    patterns = [
        (r"eyJ[A-Za-z0-9_\-]{20,}", "Supabase JWT-like string"),
    ]
    exclude_dirs = {".venv", ".git", "node_modules", "__pycache__", ".pytest_cache"}
    # Files that legitimately contain dummy JWTs for testing the JWT decoder.
    exclude_files = {
        "scripts/render_check.py",  # contains dummy JWT for the JWT decoder test
        "scripts/smoke_e2e.py",     # same
        "scripts/functional_check.py",  # same
        "scripts/verify_fixes.py",  # same
        "templates/tools/jwt_decoder.html",  # example JWT in the UI
    }
    for path in ROOT.rglob("*.py"):
        if any(part in exclude_dirs for part in path.parts):
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in exclude_files:
            continue
        contents = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, description in patterns:
            matches = re.findall(pattern, contents)
            for m in matches:
                # Allow placeholders that are clearly not real.
                if m.endswith("...") or "your-" in m.lower() or "XXXX" in m:
                    continue
                # Allow dummy JWTs with fake signatures.
                if "signature" in m.lower() or "abc123" in m.lower():
                    continue
                # Allow the standard test JWT in test files.
                if "123" in m and "test" in str(path).lower():
                    continue
                pytest.fail(
                    f"{path} contains a hardcoded credential-looking string "
                    f"({description}): {m[:30]}..."
                )


def test_env_example_contains_only_placeholders():
    """The .env.example must contain only placeholders, never real
    credentials. Real values are set via Render's secret store."""
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    # No real Supabase anon/service-role keys (they start with eyJ...).
    matches = re.findall(r"eyJ[A-Za-z0-9_\-]{20,}", env_example)
    assert not matches, (
        f".env.example contains JWT-like strings that look like real keys: {matches}"
    )
    # No real DATABASE_URL with a non-placeholder password.
    matches = re.findall(r"postgres://postgres:([^@\s]+)@db\.", env_example)
    for m in matches:
        assert m in ("PASS", "PASSWORD", "YOUR_URL_ENCODED_PASSWORD"), (
            f".env.example contains a non-placeholder password: {m}"
        )


# ── 3. Service-role key never leaks to the browser ────────────────────────

def test_service_role_key_never_in_templates():
    """The service-role key must NEVER appear in any Jinja template."""
    tmpl_dir = ROOT / "templates"
    for path in tmpl_dir.rglob("*.html"):
        contents = path.read_text(encoding="utf-8")
        assert "service_role" not in contents.lower(), (
            f"{path} contains a reference to service_role — this is suspicious."
        )
        assert "SUPABASE_SERVICE_ROLE_KEY" not in contents, (
            f"{path} contains a reference to SUPABASE_SERVICE_ROLE_KEY — "
            f"this MUST NEVER be exposed to the browser."
        )
        # The template must not directly dump config.SUPABASE_SERVICE_ROLE_KEY.
        assert "config.SUPABASE_SERVICE_ROLE_KEY" not in contents, (
            f"{path} references config.SUPABASE_SERVICE_ROLE_KEY — this MUST NOT "
            f"be rendered into the browser."
        )


def test_service_role_key_never_in_response_payloads():
    """The service-role key must NOT appear in any JSON response
    payload. We verify by reading the route handlers and checking
    that jsonify(...) calls never include 'service_role_key'."""
    routes_dir = ROOT / "routes"
    for path in routes_dir.rglob("*.py"):
        contents = path.read_text(encoding="utf-8")
        # No jsonify({"service_role_key": ...}) or similar.
        assert "service_role_key" not in contents.lower(), (
            f"{path} references service_role_key — this is suspicious."
        )


# ── 4. Migrations preserve existing RLS / audit triggers ──────────────────

def test_migrations_do_not_disable_rls():
    """No migration may DISABLE ROW LEVEL SECURITY. Existing RLS
    policies must be preserved."""
    migrations_dir = ROOT / "migrations"
    for path in migrations_dir.glob("*.sql"):
        contents = path.read_text(encoding="utf-8")
        # DISABLE ROW LEVEL SECURITY is forbidden unless explicitly
        # called out as part of a DROP-and-recreate in the rollback
        # guidance (which we don't have).
        assert "DISABLE ROW LEVEL SECURITY" not in contents.upper(), (
            f"{path} contains DISABLE ROW LEVEL SECURITY — this would "
            f"disable existing RLS protections."
        )


def test_migrations_do_not_drop_audit_triggers():
    """No migration may DROP TRIGGER on audit_* triggers."""
    migrations_dir = ROOT / "migrations"
    for path in migrations_dir.glob("*.sql"):
        contents = path.read_text(encoding="utf-8")
        # DROP TRIGGER ... audit_... is forbidden.
        # The only allowed DROP TRIGGERs are in rollback comments.
        non_comment_lines = []
        for line in contents.split("\n"):
            stripped = line.strip()
            if stripped.startswith("--"):
                continue
            non_comment_lines.append(stripped)
        joined = "\n".join(non_comment_lines)
        assert not re.search(
            r"DROP\s+TRIGGER\s+\S*audit", joined, re.IGNORECASE
        ), (
            f"{path} contains DROP TRIGGER on an audit_* trigger — this would "
            f"destroy existing audit infrastructure."
        )


def test_migrations_do_not_drop_admin_profiles_or_support_queries():
    """No PRODUCTION migration may DROP TABLE on existing public tables.

    ``000_base_schema.sql`` is a test fixture, not a production
    migration; it CREATEs the tables (it doesn't DROP them), so the
    drop check would never match anyway.
    """
    migrations_dir = ROOT / "migrations"
    protected_tables = (
        "admin_profiles", "visitor_sessions", "analytics_events",
        "analytics_daily", "support_queries", "security_events",
        "audit_log",
    )
    for path in migrations_dir.glob("*.sql"):
        if path.name.startswith("000_"):
            continue
        contents = path.read_text(encoding="utf-8")
        # Strip comment lines.
        non_comment_lines = []
        for line in contents.split("\n"):
            if not line.strip().startswith("--"):
                non_comment_lines.append(line)
        joined = "\n".join(non_comment_lines)
        for tbl in protected_tables:
            assert not re.search(
                rf"DROP\s+TABLE\s+\S*{tbl}\b", joined, re.IGNORECASE
            ), (
                f"{path} contains DROP TABLE on protected table {tbl} — "
                f"this would destroy existing data."
            )


def test_migrations_do_not_grant_anon_access_to_sensitive_tables():
    """No PRODUCTION migration may GRANT access to anon/authenticated on
    the private.delivery_queue or private.audit_log tables.

    ``000_base_schema.sql`` is a test fixture; it REVOKEs from anon and
    GRANTs to service_role and authenticated (the latter for
    is_admin, which the existing Supabase schema also grants).
    """
    migrations_dir = ROOT / "migrations"
    sensitive_tables = (
        "delivery_queue", "audit_log",
    )
    for path in migrations_dir.glob("*.sql"):
        if path.name.startswith("000_"):
            continue
        contents = path.read_text(encoding="utf-8")
        # Find GRANT statements that target anon or authenticated.
        grants = re.findall(
            r"GRANT\s+\S+\s+ON\s+(?:TABLE\s+|SEQUENCE\s+)?(\S+)\s+TO\s+(anon|authenticated|public)",
            contents, re.IGNORECASE
        )
        for table, role in grants:
            table_name = table.split(".")[-1].strip('"').lower()
            if table_name in sensitive_tables:
                pytest.fail(
                    f"{path} grants {role} access to sensitive table {table_name}"
                )


def test_migrations_preserve_admin_authorization():
    """The private.is_admin() function and admin_profiles table
    must NOT be modified by any PRODUCTION migration.

    ``000_base_schema.sql`` is excluded — it's a test fixture that
    reproduces the existing schema for the disposable PostgreSQL
    e2e tests in ``tests/test_migrations_e2e.py``. It is NOT applied
    to production.
    """
    migrations_dir = ROOT / "migrations"
    for path in migrations_dir.glob("*.sql"):
        if path.name.startswith("000_"):
            # Test fixture, not a production migration.
            continue
        contents = path.read_text(encoding="utf-8")
        # No CREATE OR REPLACE FUNCTION private.is_admin.
        # (We allow verification via SELECT but not redefinition.)
        assert not re.search(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+private\.is_admin",
            contents, re.IGNORECASE
        ), (
            f"{path} redefines private.is_admin() — this would overwrite "
            f"the existing authorization model."
        )
        # No ALTER TABLE ... ALTER COLUMN user_id on admin_profiles.
        assert not re.search(
            r"ALTER\s+TABLE\s+public\.admin_profiles\s+.*ALTER\s+COLUMN\s+user_id",
            contents, re.IGNORECASE | re.DOTALL
        ), (
            f"{path} alters admin_profiles.user_id — this would break "
            f"the foreign key to auth.users."
        )


# ── 5. No false claims about live connectivity ──────────────────────────

def test_healthz_does_not_claim_db_connectivity():
    """The public /healthz endpoint must NOT claim DB connectivity.

    It returns only ``{"ok": True}`` — whether the DB is reachable is
    a separate concern surfaced at /admin/queue (behind admin auth).
    """
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    # Find the healthz function body.
    match = re.search(
        r'@app\.get\("/healthz"\)\s*\n\s*def\s+healthz\(\):.*?(?=\n    @app|\n    def |\Z)',
        app_py, re.DOTALL
    )
    assert match, "Could not find healthz function in app.py"
    body = match.group(0)
    # It must NOT call supabase_client.ping() or reference DATABASE_URL.
    assert "ping()" not in body, (
        "healthz must not call supabase_client.ping() — that would expose "
        "DB connectivity to unauthenticated callers."
    )
    assert "DATABASE_URL" not in body, (
        "healthz must not reference DATABASE_URL."
    )
    # It must return {"ok": True}.
    assert '{"ok": True}' in body or "{'ok': True}" in body, (
        "healthz must return {'ok': True} and nothing else."
    )


def test_admin_queue_status_requires_admin_auth():
    """The /admin/queue route must require admin auth — it must NOT
    be publicly accessible."""
    routes_admin = (ROOT / "routes" / "admin.py").read_text(encoding="utf-8")
    # The queue_status function must be decorated with @auth_admin.require_admin.
    match = re.search(
        r'@admin_bp\.get\("/queue"\)\s*\n\s*@auth_admin\.require_admin\s*\n\s*def\s+queue_status',
        routes_admin
    )
    assert match, (
        "/admin/queue must be decorated with @auth_admin.require_admin — "
        "otherwise the queue contents (which include pending contact "
        "submissions) would be publicly accessible."
    )
