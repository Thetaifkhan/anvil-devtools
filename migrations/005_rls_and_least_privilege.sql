-- ─────────────────────────────────────────────────────────────────────────────
-- 005_rls_and_least_privilege.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Declares the access model in code. Earlier migrations did not enable RLS on
-- the application tables and granted EXECUTE on a SECURITY DEFINER function to
-- every signed-in user, so the repository could not reproduce the secured
-- state it documents.
--
-- Model
--   * anon and PUBLIC: no direct table access, no function execution.
--   * authenticated: read ONLY when private.is_admin() is true. Admin status
--     is decided by public.admin_profiles, not by the JWT's claims.
--   * service_role: bypasses RLS (Supabase default). Used by the delivery
--     worker and the Flask app's server-side connection.
--   * public.upsert_analytics_daily: service_role only. Before this, any
--     signed-in user could call it through PostgREST and write analytics rows.
--
-- Idempotent: safe to re-run. Policies are dropped and recreated by name, and
-- REVOKE/GRANT are repeatable. It does NOT drop tables or delete rows.
--
-- NOT applied to production automatically. Review the diff against the live
-- catalog first (see docs/production-readiness-report.md, "Apply procedure").
--
-- Rollback guidance
--   Emergency removal of RLS is a MANUAL operator action, never a migration.
--   Drop the policies listed below by name, then turn RLS off per table only
--   after an explicit decision recorded in docs/production-readiness-report.md.
--   GRANT ... restores whatever privileges were previously held. Record the
--   pre-migration grants (\dp in psql) before applying.
-- ─────────────────────────────────────────────────────────────────────────────


BEGIN;

-- ── 1. Row Level Security on every application table ─────────────────────────
ALTER TABLE public.admin_profiles    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.visitor_sessions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analytics_events  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analytics_daily   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.support_queries   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.security_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE private.audit_log        ENABLE ROW LEVEL SECURITY;

-- ── 2. Remove direct access from browser-facing roles ───────────────────────
REVOKE ALL ON public.admin_profiles    FROM PUBLIC, anon;
REVOKE ALL ON public.visitor_sessions  FROM PUBLIC, anon;
REVOKE ALL ON public.analytics_events  FROM PUBLIC, anon;
REVOKE ALL ON public.analytics_daily   FROM PUBLIC, anon;
REVOKE ALL ON public.support_queries   FROM PUBLIC, anon;
REVOKE ALL ON public.security_events   FROM PUBLIC, anon;
REVOKE ALL ON private.audit_log        FROM PUBLIC, anon, authenticated;

REVOKE ALL ON public.admin_profiles    FROM authenticated;
REVOKE ALL ON public.visitor_sessions  FROM authenticated;
REVOKE ALL ON public.analytics_events  FROM authenticated;
REVOKE ALL ON public.analytics_daily   FROM authenticated;
REVOKE ALL ON public.support_queries   FROM authenticated;
REVOKE ALL ON public.security_events   FROM authenticated;

-- ── 2b. Schema USAGE: RLS policies call private.is_admin(), so the invoking
-- role needs USAGE on the private schema or every policy errors. USAGE grants
-- only name visibility; table and function privileges above still apply.
REVOKE ALL ON SCHEMA private FROM PUBLIC, anon;
GRANT  USAGE ON SCHEMA private TO authenticated, service_role;

-- ── 3. authenticated: read-only, and only for admins (via RLS) ──────────────
GRANT SELECT ON public.admin_profiles    TO authenticated;
GRANT SELECT ON public.visitor_sessions  TO authenticated;
GRANT SELECT ON public.analytics_events  TO authenticated;
GRANT SELECT ON public.analytics_daily   TO authenticated;
GRANT SELECT ON public.support_queries   TO authenticated;
GRANT SELECT ON public.security_events   TO authenticated;

-- Admins may update ONLY the editable support columns, never the audit trail
-- or identity fields. Column-level grants keep this narrow.
GRANT UPDATE (status, priority, admin_notes, resolved_at, updated_at)
    ON public.support_queries TO authenticated;

DROP POLICY IF EXISTS admin_read_admin_profiles   ON public.admin_profiles;
DROP POLICY IF EXISTS admin_read_visitor_sessions ON public.visitor_sessions;
DROP POLICY IF EXISTS admin_read_analytics_events ON public.analytics_events;
DROP POLICY IF EXISTS admin_read_analytics_daily  ON public.analytics_daily;
DROP POLICY IF EXISTS admin_read_support_queries  ON public.support_queries;
DROP POLICY IF EXISTS admin_read_security_events  ON public.security_events;
DROP POLICY IF EXISTS admin_update_support_queries ON public.support_queries;

CREATE POLICY admin_read_admin_profiles ON public.admin_profiles
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_read_visitor_sessions ON public.visitor_sessions
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_read_analytics_events ON public.analytics_events
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_read_analytics_daily ON public.analytics_daily
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_read_support_queries ON public.support_queries
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_read_security_events ON public.security_events
    FOR SELECT TO authenticated USING (private.is_admin());
CREATE POLICY admin_update_support_queries ON public.support_queries
    FOR UPDATE TO authenticated
    USING (private.is_admin())
    WITH CHECK (private.is_admin());

-- ── 4. Functions: remove default EXECUTE, grant to the minimum role ─────────
-- Guarded on existence so this file also applies on its own. Functions created
-- by 003/004 are only present when those migrations have run.
DO $$
BEGIN
    IF to_regprocedure('public.upsert_analytics_daily(date,text,bigint,bigint,bigint,bigint)') IS NOT NULL THEN
        REVOKE EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint)
            FROM PUBLIC, anon, authenticated;
        GRANT  EXECUTE ON FUNCTION public.upsert_analytics_daily(date, text, bigint, bigint, bigint, bigint)
            TO service_role;
    END IF;
    IF to_regprocedure('private.claim_delivery_queue(integer)') IS NOT NULL THEN
        REVOKE EXECUTE ON FUNCTION private.claim_delivery_queue(integer)
            FROM PUBLIC, anon, authenticated;
        GRANT  EXECUTE ON FUNCTION private.claim_delivery_queue(integer) TO service_role;
    END IF;
END $$;

-- private.is_admin() must stay executable by authenticated: the RLS policies
-- above call it. It only answers "is the caller an admin", so this is safe.
REVOKE EXECUTE ON FUNCTION private.is_admin() FROM PUBLIC, anon;
GRANT  EXECUTE ON FUNCTION private.is_admin() TO authenticated, service_role;

COMMIT;
