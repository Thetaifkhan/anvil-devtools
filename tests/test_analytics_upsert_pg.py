"""Analytics aggregation upsert, verified on a REAL PostgreSQL server.

Covers the mandate's required behaviours:
  * clean migration run, and the 004 -> 006 upgrade path
  * first aggregation inserts the expected row
  * repeated aggregation updates in place (idempotent recompute)
  * site-wide and tool-specific rows stay distinct
  * NULL, '' and whitespace slugs normalize to the same site-wide row
  * invalid (negative / NULL) counts are rejected
  * concurrent writers on the same key produce one logical row

Runs against the disposable server on PG_PORT (default 55432). Skips when that
server is unreachable. Nothing here touches a live Supabase project.
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
DB = "anvil_upsert_test"
DAY = "2026-10-01"


def _psql():
    if (PG_INSTALL_DIR / "bin" / "psql").exists():
        return str(PG_INSTALL_DIR / "bin" / "psql")
    return shutil.which("psql")


def _run(sql=None, *, db=DB, file=None):
    cmd = [_psql(), "-h", "/var/run/postgresql", "-p", str(PG_PORT), "-U", "postgres",
           "-d", db, "-v", "ON_ERROR_STOP=1", "-tA"]
    cmd += (["-f", str(file)] if file else ["-c", sql])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120)


def _server_up():
    p = _psql()
    if not p:
        return False
    r = subprocess.run([p, "-h", "/var/run/postgresql", "-p", str(PG_PORT), "-U", "postgres",
                        "-d", "postgres", "-tA", "-c", "SELECT 1"],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not (_psql() and _server_up()),
                                reason="disposable PostgreSQL not reachable")

MIGRATION_ORDER = [
    "000_base_schema.sql",  # test-only reproduction of the pre-existing schema
    "001_schema_verification.sql",
    "002_missing_constraints_and_indexes.sql",
    "003_retry_queue_idempotency.sql",
    "004_admin_analytics_support.sql",
    "005_rls_and_least_privilege.sql",
    "006_analytics_upsert_normalization.sql",
]


def _fresh_db(migrations):
    _run(f"DROP DATABASE IF EXISTS {DB};", db="postgres")
    assert _run(f"CREATE DATABASE {DB};", db="postgres").returncode == 0
    for name in migrations:
        r = _run(file=MIGRATIONS / name)
        assert r.returncode == 0, f"{name} failed: {r.stderr}"


def _upsert(slug_sql, views=10, visitors=3, execs=2, errs=0, day=DAY):
    return _run(f"SELECT public.upsert_analytics_daily('{day}', {slug_sql}, "
                f"{views}, {visitors}, {execs}, {errs});")


@pytest.fixture(scope="module")
def full_db():
    _fresh_db(MIGRATION_ORDER)
    yield


class TestMigrationRun:
    def test_clean_apply_of_every_migration(self):
        _fresh_db(MIGRATION_ORDER)  # assertion inside raises on any failure

    def test_upgrade_path_004_then_006(self):
        """A database already at 004 (live state) upgrades to 006 cleanly."""
        _fresh_db(MIGRATION_ORDER[:5])
        r = _run(file=MIGRATIONS / "006_analytics_upsert_normalization.sql")
        assert r.returncode == 0, r.stderr

    def test_006_is_idempotent(self):
        _fresh_db(MIGRATION_ORDER)
        r = _run(file=MIGRATIONS / "006_analytics_upsert_normalization.sql")
        assert r.returncode == 0, r.stderr


class TestUpsertSemantics:
    def test_first_aggregation_inserts_expected_row(self, full_db):
        _fresh_db(MIGRATION_ORDER)
        assert _upsert("'json-formatter'", views=10, visitors=3, execs=2, errs=1).returncode == 0
        r = _run("SELECT page_views, estimated_unique_visitors, tool_executions, tool_errors "
                 "FROM public.analytics_daily WHERE tool_slug='json-formatter';")
        assert r.stdout.strip() == "10|3|2|1"

    def test_repeat_aggregation_updates_in_place(self, full_db):
        _fresh_db(MIGRATION_ORDER)
        _upsert("'json-formatter'", views=10)
        _upsert("'json-formatter'", views=10)  # identical recompute
        _upsert("'json-formatter'", views=25)  # later total for the same day
        n = _run("SELECT count(*) FROM public.analytics_daily WHERE tool_slug='json-formatter';")
        assert n.stdout.strip() == "1", "repeat aggregation created a duplicate row"
        v = _run("SELECT page_views FROM public.analytics_daily WHERE tool_slug='json-formatter';")
        assert v.stdout.strip() == "25", "totals were not updated"

    def test_site_wide_and_tool_rows_are_distinct(self, full_db):
        _fresh_db(MIGRATION_ORDER)
        _upsert("NULL", views=100)
        _upsert("'json-formatter'", views=10)
        n = _run("SELECT count(*) FROM public.analytics_daily WHERE metric_date='2026-10-01';")
        assert n.stdout.strip() == "2"
        site = _run("SELECT page_views FROM public.analytics_daily WHERE tool_slug IS NULL;")
        assert site.stdout.strip() == "100"

    @pytest.mark.parametrize("empty", ["NULL", "''", "'   '"])
    def test_empty_slugs_normalize_to_one_site_wide_row(self, empty):
        """Regression: '' was stored as its own key, creating a logical duplicate."""
        _fresh_db(MIGRATION_ORDER)
        _upsert("NULL", views=5)
        _upsert(empty, views=7)
        n = _run("SELECT count(*) FROM public.analytics_daily WHERE tool_slug IS NULL;")
        assert n.stdout.strip() == "1", f"slug {empty} created a second site-wide row"
        assert _run("SELECT count(*) FROM public.analytics_daily WHERE tool_slug = '';"
                    ).stdout.strip() == "0"

    def test_slug_is_trimmed_before_keying(self):
        _fresh_db(MIGRATION_ORDER)
        _upsert("'json-formatter'", views=1)
        _upsert("'  json-formatter  '", views=2)
        n = _run("SELECT count(*) FROM public.analytics_daily;")
        assert n.stdout.strip() == "1"

    @pytest.mark.parametrize("bad", [
        "-1", "NULL",
    ])
    def test_invalid_counts_are_rejected(self, bad):
        _fresh_db(MIGRATION_ORDER)
        r = _run(f"SELECT public.upsert_analytics_daily('{DAY}', 'x', {bad}, 1, 1, 0);")
        assert r.returncode != 0, "invalid page_views was accepted"
        assert _run("SELECT count(*) FROM public.analytics_daily;").stdout.strip() == "0"

    def test_null_metric_date_is_rejected(self):
        _fresh_db(MIGRATION_ORDER)
        r = _run("SELECT public.upsert_analytics_daily(NULL, 'x', 1, 1, 1, 0);")
        assert r.returncode != 0


class TestConcurrency:
    def test_concurrent_writers_produce_one_logical_row(self, tmp_path):
        """Eight concurrent sessions upsert the same keys repeatedly. The unique
        expression index plus ON CONFLICT must leave exactly one row per key."""
        _fresh_db(MIGRATION_ORDER)
        script = tmp_path / "upsert_hammer.sql"
        lines = []
        for i in range(25):
            lines.append(f"SELECT public.upsert_analytics_daily('{DAY}', NULL, {i}, 1, 1, 0);")
            lines.append(f"SELECT public.upsert_analytics_daily('{DAY}', 'json-formatter', {i}, 1, 1, 0);")
            lines.append(f"SELECT public.upsert_analytics_daily('{DAY}', '', {i}, 1, 1, 0);")
        script.write_text("\n".join(lines) + "\n")
        try:
            procs = [subprocess.Popen(
                [_psql(), "-h", "/var/run/postgresql", "-p", str(PG_PORT), "-U", "postgres",
                 "-d", DB, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(script)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
            results = [p.communicate(timeout=180) for p in procs]
        finally:
            pass
        for p, (out, err) in zip(procs, results):
            assert p.returncode == 0, f"a writer failed: {err}"
        r = _run("SELECT count(*) FROM public.analytics_daily;")
        assert r.stdout.strip() == "2", "concurrent upserts created duplicate logical rows"
        site = _run("SELECT count(*) FROM public.analytics_daily WHERE tool_slug IS NULL;")
        assert site.stdout.strip() == "1"
