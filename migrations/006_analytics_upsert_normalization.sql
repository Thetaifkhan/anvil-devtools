-- ─────────────────────────────────────────────────────────────────────────────
-- 006_analytics_upsert_normalization.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Forward-only correction to public.upsert_analytics_daily() from 004.
--
-- Defects in the 004 version (confirmed by reproduction on PostgreSQL 16):
--   1. An empty or whitespace tool slug ('' or '   ') was stored as-is. The
--      unique index keys on COALESCE(tool_slug, '<site-wide>'), which does not
--      map '' to the site-wide key. A caller passing '' therefore created a
--      second, logically duplicate site-wide row.
--   2. Negative or NULL counts were accepted and would corrupt dashboards.
--
-- The fix normalizes the slug with NULLIF(btrim(...), '') before the write, so
-- NULL, '' and whitespace all mean "site-wide" and store NULL. The conflict
-- target is unchanged and still matches the index expression exactly.
--
-- Semantics (unchanged): the function SETS absolute daily totals. Callers must
-- pass the full day's totals. Calling it repeatedly with the same totals is
-- idempotent. Passing per-batch deltas would overwrite data instead.
--
-- Idempotent: CREATE OR REPLACE, with grants re-applied. Creates no tables and
-- deletes no rows. Safe to run after 004. Applying it before 004 fails in the
-- DO block, which is intended.
--
-- Rollback: re-run the function body from 004 (see migrations/004). Nothing in
-- this migration changes stored data.
-- ─────────────────────────────────────────────────────────────────────────────

BEGIN;

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
DECLARE
    v_slug text := NULLIF(btrim(p_tool_slug), '');
BEGIN
    IF p_metric_date IS NULL THEN
        RAISE EXCEPTION 'metric_date is required' USING ERRCODE = '22004';
    END IF;
    IF p_page_views IS NULL OR p_page_views < 0
       OR p_estimated_unique_visitors IS NULL OR p_estimated_unique_visitors < 0
       OR p_tool_executions IS NULL OR p_tool_executions < 0
       OR p_tool_errors IS NULL OR p_tool_errors < 0 THEN
        RAISE EXCEPTION 'analytics counts must be non-negative and non-null'
            USING ERRCODE = '22003';
    END IF;

    INSERT INTO public.analytics_daily (
        metric_date, tool_slug, page_views, estimated_unique_visitors,
        tool_executions, tool_errors, updated_at
    )
    VALUES (
        p_metric_date, v_slug, p_page_views, p_estimated_unique_visitors,
        p_tool_executions, p_tool_errors, now()
    )
    ON CONFLICT (metric_date, (COALESCE(tool_slug, '<site-wide>')))
    DO UPDATE SET
        page_views = EXCLUDED.page_views,
        estimated_unique_visitors = EXCLUDED.estimated_unique_visitors,
        tool_executions = EXCLUDED.tool_executions,
        tool_errors = EXCLUDED.tool_errors,
        updated_at = now();
END;
$$;

REVOKE EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT  EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint)
    TO service_role;

COMMIT;
