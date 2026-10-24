-- ─────────────────────────────────────────────────────────────────────────────
-- 004_admin_analytics_support.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds the optional columns the admin dashboard and contact form need on
-- the existing public.support_queries table, plus a few check constraints
-- and audit trigger verification.
--
-- Every ALTER is conditional on the column not existing already, so the
-- file is safe to re-run.
--
-- Apply with EITHER of the following:
--
-- 1. psql (terminal, recommended for scripted deploys):
--      psql "$DATABASE_URL" \
--        -v ON_ERROR_STOP=1 \
--        -f migrations/004_admin_analytics_support.sql
--
-- 2. Supabase SQL Editor (Dashboard → SQL → New Query):
--    Paste the file's contents (the ``\set ON_ERROR_STOP on`` line is a
--    psql meta-command and is silently ignored by the SQL Editor; you do
--    NOT need to delete it). Click RUN. The Editor reports each
--    NOTICE/EXCEPTION line-by-line. If any statement fails, fix the
--    issue and re-run the whole file — every block is idempotent.
-- ─────────────────────────────────────────────────────────────────────────────


BEGIN;

-- ── support_queries: optional columns ───────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'support_queries'
          AND column_name = 'reference_code'
    ) THEN
        ALTER TABLE public.support_queries ADD COLUMN reference_code TEXT;
        RAISE NOTICE 'Added support_queries.reference_code.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'support_queries'
          AND column_name = 'tool_slug'
    ) THEN
        ALTER TABLE public.support_queries ADD COLUMN tool_slug TEXT
            CHECK (tool_slug IS NULL OR char_length(tool_slug) <= 100);
        RAISE NOTICE 'Added support_queries.tool_slug.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'support_queries'
          AND column_name = 'browser_context'
    ) THEN
        ALTER TABLE public.support_queries ADD COLUMN browser_context JSONB
            DEFAULT '{}'::jsonb
            CHECK (jsonb_typeof(browser_context) = 'object'
                   AND pg_column_size(browser_context) <= 4096);
        RAISE NOTICE 'Added support_queries.browser_context.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'support_queries'
          AND column_name = 'consent_recorded_at'
    ) THEN
        ALTER TABLE public.support_queries ADD COLUMN consent_recorded_at TIMESTAMPTZ;
        RAISE NOTICE 'Added support_queries.consent_recorded_at.';
    END IF;
END $$;

-- reference_code unique index (NULLs allowed, multiple NULLs allowed)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'support_queries'
          AND indexname = 'support_queries_reference_code_key'
    ) THEN
        EXECUTE $stmt$
            CREATE UNIQUE INDEX support_queries_reference_code_key
                ON public.support_queries (reference_code)
                WHERE reference_code IS NOT NULL
        $stmt$;
        RAISE NOTICE 'Created support_queries_reference_code_key.';
    END IF;
END $$;

-- ── support_queries: tighten category / status / priority if not already ────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'support_queries_category_check'
    ) THEN
        ALTER TABLE public.support_queries
            ADD CONSTRAINT support_queries_category_check
            CHECK (category IN (
                'bug_report', 'feature_request', 'general_question',
                'feedback', 'other'
            ));
        RAISE NOTICE 'Added support_queries_category_check.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'support_queries_status_check'
    ) THEN
        ALTER TABLE public.support_queries
            ADD CONSTRAINT support_queries_status_check
            CHECK (status IN ('new', 'in_progress', 'resolved', 'closed'));
        RAISE NOTICE 'Added support_queries_status_check.';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'support_queries_priority_check'
    ) THEN
        ALTER TABLE public.support_queries
            ADD CONSTRAINT support_queries_priority_check
            CHECK (priority IN ('low', 'normal', 'high'));
        RAISE NOTICE 'Added support_queries_priority_check.';
    END IF;
END $$;

-- ── Verify the audit trigger is still in place on support_queries ───────────
DO $$
DECLARE
    trigger_count integer;
BEGIN
    SELECT count(*) INTO trigger_count
    FROM information_schema.triggers
    WHERE event_object_schema = 'public'
      AND event_object_table = 'support_queries'
      AND (trigger_name LIKE 'audit_%' OR trigger_name LIKE '%audit%');

    IF trigger_count = 0 THEN
        RAISE WARNING 'No audit trigger found on public.support_queries. '
                      'Re-apply the project''s original audit-trigger migration.';
    ELSE
        RAISE NOTICE 'Audit trigger(s) on public.support_queries present (%).', trigger_count;
    END IF;
END $$;

-- ── Verify the audit trigger on admin_profiles ────────────────────────────
DO $$
DECLARE
    trigger_count integer;
BEGIN
    SELECT count(*) INTO trigger_count
    FROM information_schema.triggers
    WHERE event_object_schema = 'public'
      AND event_object_table = 'admin_profiles'
      AND (trigger_name LIKE 'audit_%' OR trigger_name LIKE '%audit%');

    IF trigger_count = 0 THEN
        RAISE WARNING 'No audit trigger found on public.admin_profiles.';
    ELSE
        RAISE NOTICE 'Audit trigger(s) on public.admin_profiles present (%).', trigger_count;
    END IF;
END $$;

-- ── analytics_daily: an upsert helper function used by the worker ─────────
-- ON CONFLICT requires a unique constraint or index. We rely on the unique
-- index created in 002, which uses COALESCE(tool_slug, '<site-wide>') so
-- NULL slugs are treated as one logical group.
--
-- SECURITY DEFINER + search_path = '' (empty) is the recommended
-- hardening for privileged functions. All referenced objects are
-- schema-qualified so the function cannot be hijacked by an attacker
-- who creates a malicious object in their search_path.
CREATE OR REPLACE FUNCTION public.upsert_analytics_daily(
    p_metric_date date,
    p_tool_slug text,
    p_page_views bigint,
    p_estimated_unique_visitors bigint,
    p_tool_executions bigint,
    p_tool_errors bigint
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    INSERT INTO public.analytics_daily (
        metric_date, tool_slug, page_views, estimated_unique_visitors,
        tool_executions, tool_errors, updated_at
    )
    VALUES (
        p_metric_date, p_tool_slug, p_page_views, p_estimated_unique_visitors,
        p_tool_executions, p_tool_errors, now()
    )
    ON CONFLICT (metric_date, COALESCE(public.analytics_daily.tool_slug, '<site-wide>'))
    DO UPDATE SET
        page_views = EXCLUDED.page_views,
        estimated_unique_visitors = EXCLUDED.estimated_unique_visitors,
        tool_executions = EXCLUDED.tool_executions,
        tool_errors = EXCLUDED.tool_errors,
        updated_at = now();
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint) TO service_role, authenticated;

COMMIT;

-- ── Rollback guidance ──────────────────────────────────────────────────────
-- DROP FUNCTION IF EXISTS public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint);
-- DROP INDEX IF EXISTS public.support_queries_reference_code_key;
-- ALTER TABLE public.support_queries DROP COLUMN IF EXISTS reference_code;
-- ALTER TABLE public.support_queries DROP COLUMN IF EXISTS tool_slug;
-- ALTER TABLE public.support_queries DROP COLUMN IF EXISTS browser_context;
-- ALTER TABLE public.support_queries DROP COLUMN IF EXISTS consent_recorded_at;
-- (Leave the audit triggers in place — they were never modified.)
