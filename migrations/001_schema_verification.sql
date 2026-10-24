-- ─────────────────────────────────────────────────────────────────────────────
-- 001_schema_verification.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Run this first. It is intentionally read-only and idempotent: every block
-- raises an exception if an expected object is missing so the operator can
-- decide what to fix manually rather than letting later migrations paper
-- over a broken schema.
--
-- Run order:
--   001_schema_verification.sql       (this file — fails loud if schema is wrong)
--   002_missing_constraints_and_indexes.sql
--   003_retry_queue_idempotency.sql
--   004_admin_analytics_support.sql
--
-- Apply with EITHER of the following:
--
-- 1. psql (terminal, recommended for scripted deploys):
--      psql "$DATABASE_URL" \
--        -v ON_ERROR_STOP=1 \
--        -f migrations/001_schema_verification.sql
--
-- 2. Supabase SQL Editor (Dashboard → SQL → New Query):
--    Paste the file's contents (the ``\set ON_ERROR_STOP on`` line is a
--    psql meta-command and is silently ignored by the SQL Editor; you do
--    NOT need to delete it). Click RUN. The Editor reports each
--    NOTICE/EXCEPTION line-by-line. If any statement fails, fix the
--    issue and re-run the whole file — every block is idempotent.
-- ─────────────────────────────────────────────────────────────────────────────


BEGIN;

-- Verify the public tables the application depends on already exist.
DO $$
DECLARE
    expected text[] := ARRAY[
        'admin_profiles',
        'visitor_sessions',
        'analytics_events',
        'analytics_daily',
        'support_queries',
        'security_events'
    ];
    missing text;
BEGIN
    FOREACH missing IN ARRAY expected LOOP
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = missing
        ) THEN
            RAISE EXCEPTION 'Missing required table: public.%', missing
                USING HINT = 'Run the original schema migration that ships with the project before applying 002–004.';
        END IF;
    END LOOP;
    RAISE NOTICE 'All expected public tables are present.';
END $$;

-- Verify the private audit infrastructure exists.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'private' AND table_name = 'audit_log'
    ) THEN
        RAISE EXCEPTION 'Missing required table: private.audit_log'
            USING HINT = 'The audit infrastructure must be created before the app can write support/admin changes.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.routines
        WHERE routine_schema = 'private' AND routine_name = 'is_admin'
    ) THEN
        RAISE EXCEPTION 'Missing required function: private.is_admin()';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.routines
        WHERE routine_schema = 'private' AND routine_name = 'capture_audit_change'
    ) THEN
        RAISE EXCEPTION 'Missing required function: private.capture_audit_change()';
    END IF;
    RAISE NOTICE 'Private audit infrastructure is present.';
END $$;

-- Sanity-check a few columns the app relies on. These checks do not alter
-- anything; they only fail loud if a column was renamed without an app
-- update.
DO $$
DECLARE
    rec record;
    missing boolean;
BEGIN
    FOR rec IN
        SELECT * FROM (VALUES
            ('public',  'visitor_sessions',  'visitor_id'),
            ('public',  'visitor_sessions',  'engagement_seconds'),
            ('public',  'visitor_sessions',  'ip_hmac'),
            ('public',  'analytics_events',   'event_name'),
            ('public',  'analytics_events',   'session_id'),
            ('public',  'analytics_events',   'properties'),
            ('public',  'analytics_daily',     'metric_date'),
            ('public',  'analytics_daily',     'tool_slug'),
            ('public',  'support_queries',    'status'),
            ('public',  'support_queries',    'priority'),
            ('public',  'security_events',     'event_type'),
            ('public',  'security_events',     'ip_hmac')
        ) AS cols(table_schema, table_name, column_name)
    LOOP
        SELECT NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE columns.table_schema = rec.table_schema
              AND columns.table_name   = rec.table_name
              AND columns.column_name  = rec.column_name
        ) INTO missing;
        IF missing THEN
            RAISE EXCEPTION 'Missing column %.%.%',
                rec.table_schema, rec.table_name, rec.column_name;
        END IF;
    END LOOP;
    RAISE NOTICE 'All expected columns are present.';
END $$;

COMMIT;

-- ── Rollback guidance ──────────────────────────────────────────────────────
-- This migration is intentionally read-only — it raises an exception if
-- any required object is missing, but it never creates or alters schema.
-- No rollback is necessary. If it failed, fix the underlying schema
-- (apply the original schema migration) and re-run this file.
