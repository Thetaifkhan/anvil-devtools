-- ─────────────────────────────────────────────────────────────────────────────
-- 002_missing_constraints_and_indexes.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds only genuinely missing constraints and indexes. Every block checks
-- for existence first so the file is safe to re-run after partial failure.
--
-- Apply with EITHER of the following:
--
-- 1. psql (terminal, recommended for scripted deploys):
--      psql "$DATABASE_URL" \
--        -v ON_ERROR_STOP=1 \
--        -f migrations/002_missing_constraints_and_indexes.sql
--
-- 2. Supabase SQL Editor (Dashboard → SQL → New Query):
--    Paste the file's contents (the ``\set ON_ERROR_STOP on`` line is a
--    psql meta-command and is silently ignored by the SQL Editor; you do
--    NOT need to delete it). Click RUN. The Editor reports each
--    NOTICE/EXCEPTION line-by-line. If any statement fails, fix the
--    issue and re-run the whole file — every block is idempotent.
-- ─────────────────────────────────────────────────────────────────────────────


BEGIN;

-- ── analytics_daily: unique index on (metric_date, tool_slug) ──────────────
-- tool_slug is nullable (a NULL slug represents site-wide metrics). We use
-- a UNIQUE constraint on (metric_date, COALESCE(tool_slug, '<site-wide>'))
-- so NULLs collapse into a single sentinel group per day.
--
-- IMPORTANT: this is implemented as a UNIQUE INDEX on an expression rather
-- than as a column-level CONSTRAINT, because PostgreSQL does not allow
-- expression-based UNIQUE constraints on the column itself. The ON CONFLICT
-- clause in the upsert_analytics_daily function (migration 004) references
-- the same expression EXACTLY — both the index definition and the ON
-- CONFLICT clause must use COALESCE(tool_slug, '<site-wide>') verbatim.
-- Any divergence (different sentinel, different whitespace, different
-- function) will cause ``ON CONFLICT`` to fail with
-- "there is no unique or exclusion constraint matching the ON CONFLICT
-- specification".
DO $$
DECLARE
    dup_count integer;
BEGIN
    SELECT count(*) INTO dup_count FROM (
        SELECT metric_date, COALESCE(tool_slug, '<site-wide>') AS k, count(*) AS n
        FROM public.analytics_daily
        GROUP BY metric_date, COALESCE(tool_slug, '<site-wide>')
        HAVING count(*) > 1
    ) s;
    IF dup_count > 0 THEN
        RAISE EXCEPTION 'Found % duplicate (metric_date, tool_slug) rows in analytics_daily. Resolve them manually before creating the unique index.', dup_count;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'analytics_daily'
          AND indexname = 'analytics_daily_metric_date_tool_slug_key'
    ) THEN
        EXECUTE $stmt$
            CREATE UNIQUE INDEX analytics_daily_metric_date_tool_slug_key
                ON public.analytics_daily (metric_date, COALESCE(tool_slug, '<site-wide>'))
        $stmt$;
        RAISE NOTICE 'Created unique index analytics_daily_metric_date_tool_slug_key.';
    ELSE
        RAISE NOTICE 'Unique index analytics_daily_metric_date_tool_slug_key already exists.';
    END IF;
END $$;

-- ── analytics_events: helper indexes for the admin dashboard ───────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'analytics_events'
          AND indexname = 'analytics_events_occurred_at_idx'
    ) THEN
        CREATE INDEX analytics_events_occurred_at_idx
            ON public.analytics_events (occurred_at DESC);
        RAISE NOTICE 'Created analytics_events_occurred_at_idx.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'analytics_events'
          AND indexname = 'analytics_events_session_id_idx'
    ) THEN
        CREATE INDEX analytics_events_session_id_idx
            ON public.analytics_events (session_id);
        RAISE NOTICE 'Created analytics_events_session_id_idx.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'analytics_events'
          AND indexname = 'analytics_events_event_name_idx'
    ) THEN
        CREATE INDEX analytics_events_event_name_idx
            ON public.analytics_events (event_name, occurred_at DESC);
        RAISE NOTICE 'Created analytics_events_event_name_idx.';
    END IF;
END $$;

-- ── visitor_sessions: helper indexes ───────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'visitor_sessions'
          AND indexname = 'visitor_sessions_last_seen_at_idx'
    ) THEN
        CREATE INDEX visitor_sessions_last_seen_at_idx
            ON public.visitor_sessions (last_seen_at DESC);
        RAISE NOTICE 'Created visitor_sessions_last_seen_at_idx.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'visitor_sessions'
          AND indexname = 'visitor_sessions_visitor_id_idx'
    ) THEN
        CREATE INDEX visitor_sessions_visitor_id_idx
            ON public.visitor_sessions (visitor_id);
        RAISE NOTICE 'Created visitor_sessions_visitor_id_idx.';
    END IF;
END $$;

-- ── support_queries: lookup indexes (status, priority, reference_code) ─────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'support_queries'
          AND indexname = 'support_queries_status_created_idx'
    ) THEN
        CREATE INDEX support_queries_status_created_idx
            ON public.support_queries (status, created_at DESC);
        RAISE NOTICE 'Created support_queries_status_created_idx.';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'support_queries'
          AND indexname = 'support_queries_priority_created_idx'
    ) THEN
        CREATE INDEX support_queries_priority_created_idx
            ON public.support_queries (priority, created_at DESC);
        RAISE NOTICE 'Created support_queries_priority_created_idx.';
    END IF;
END $$;

-- ── support_queries.reference_code unique index ─────────────────────────────
-- reference_code may not exist as a column on every existing schema. Inspect
-- before adding; if absent, 004_admin_analytics_support.sql adds the column
-- and we add the unique index there.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'support_queries'
          AND column_name = 'reference_code'
    ) AND NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'support_queries'
          AND indexname = 'support_queries_reference_code_key'
    ) THEN
        EXECUTE $stmt$
            CREATE UNIQUE INDEX support_queries_reference_code_key
                ON public.support_queries (reference_code)
                WHERE reference_code IS NOT NULL
        $stmt$;
        RAISE NOTICE 'Created unique index support_queries_reference_code_key.';
    ELSE
        RAISE NOTICE 'support_queries.reference_code or its unique index not applicable here.';
    END IF;
END $$;

-- ── security_events: index by event_type + occurred_at ─────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'security_events'
          AND indexname = 'security_events_event_type_occurred_at_idx'
    ) THEN
        CREATE INDEX security_events_event_type_occurred_at_idx
            ON public.security_events (event_type, occurred_at DESC);
        RAISE NOTICE 'Created security_events_event_type_occurred_at_idx.';
    END IF;
END $$;

COMMIT;

-- ── Rollback guidance ──────────────────────────────────────────────────────
-- DROP INDEX IF EXISTS public.analytics_daily_metric_date_tool_slug_key;
-- DROP INDEX IF EXISTS public.analytics_events_occurred_at_idx;
-- DROP INDEX IF EXISTS public.analytics_events_session_id_idx;
-- DROP INDEX IF EXISTS public.analytics_events_event_name_idx;
-- DROP INDEX IF EXISTS public.visitor_sessions_last_seen_at_idx;
-- DROP INDEX IF EXISTS public.visitor_sessions_visitor_id_idx;
-- DROP INDEX IF EXISTS public.support_queries_status_created_idx;
-- DROP INDEX IF EXISTS public.support_queries_priority_created_idx;
-- DROP INDEX IF EXISTS public.support_queries_reference_code_key;
-- DROP INDEX IF EXISTS public.security_events_event_type_occurred_at_idx;
