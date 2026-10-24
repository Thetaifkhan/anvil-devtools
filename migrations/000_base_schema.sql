-- ─────────────────────────────────────────────────────────────────────────────
-- 000_base_schema.sql — the existing schema the migrations expect to find.
-- ─────────────────────────────────────────────────────────────────────────────
-- This file is used ONLY by the integration test (tests/test_migrations_e2e.py).
-- It is NOT applied to production. The operator already has these tables
-- in their Supabase database; the migrations 001-004 build ON TOP of them.
--
-- This is a faithful reproduction of the schema described in the project
-- README/spec section 2 (Existing schema). It exists so the test harness
-- can run the migrations against a fresh disposable database and verify
-- they apply cleanly without touching any real Supabase project.
-- ─────────────────────────────────────────────────────────────────────────────


-- ── Schemas ─────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS private;
CREATE SCHEMA IF NOT EXISTS public;
-- In real Supabase deployments, the `auth` schema and the `anon`,
-- `authenticated`, and `service_role` roles are provided by the Supabase
-- platform. For the disposable test database we create stubs so the
-- REVOKE/GRANT statements in the migrations don't fail. Production
-- already has these roles.
CREATE SCHEMA IF NOT EXISTS auth;
CREATE OR REPLACE FUNCTION auth.uid()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$ SELECT NULLIF(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
REVOKE EXECUTE ON FUNCTION auth.uid() FROM PUBLIC;
-- Stub roles for the disposable test DB.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role NOLOGIN BYPASSRLS;
    END IF;
END $$;

-- ── public.admin_profiles ───────────────────────────────────────────────────
CREATE TABLE public.admin_profiles (
    user_id UUID PRIMARY KEY,
    display_name TEXT
        CHECK (
            display_name IS NULL
            OR char_length(display_name) <= 120
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── public.visitor_sessions ─────────────────────────────────────────────────
CREATE TABLE public.visitor_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    visitor_id UUID NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    engagement_seconds INTEGER NOT NULL DEFAULT 0
        CHECK (
            engagement_seconds >= 0
            AND engagement_seconds <= 604800
        ),
    landing_page TEXT
        CHECK (
            landing_page IS NULL
            OR char_length(landing_page) <= 2048
        ),
    referrer_domain TEXT
        CHECK (
            referrer_domain IS NULL
            OR char_length(referrer_domain) <= 253
        ),
    device_type TEXT
        CHECK (
            device_type IS NULL
            OR device_type IN (
                'desktop', 'mobile', 'tablet', 'unknown'
            )
        ),
    country_code CHAR(2)
        CHECK (
            country_code IS NULL
            OR country_code ~ '^[A-Z]{2}$'
        ),
    ip_hmac CHAR(64)
        CHECK (
            ip_hmac IS NULL
            OR ip_hmac ~ '^[0-9a-f]{64}$'
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── public.analytics_events ────────────────────────────────────────────────
CREATE TABLE public.analytics_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_name TEXT NOT NULL CHECK (
        event_name IN (
            'page_view',
            'tool_run',
            'tool_success',
            'tool_error',
            'query_submitted',
            'search',
            'feature_request_vote'
        )
    ),
    session_id UUID REFERENCES public.visitor_sessions(id),
    tool_slug TEXT
        CHECK (
            tool_slug IS NULL
            OR char_length(tool_slug) <= 100
        ),
    page_path TEXT
        CHECK (
            page_path IS NULL
            OR char_length(page_path) <= 2048
        ),
    properties JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (
            jsonb_typeof(properties) = 'object'
            AND pg_column_size(properties) <= 4096
        ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── public.analytics_daily ──────────────────────────────────────────────────
CREATE TABLE public.analytics_daily (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    metric_date DATE NOT NULL,
    tool_slug TEXT
        CHECK (
            tool_slug IS NULL
            OR char_length(tool_slug) <= 100
        ),
    page_views BIGINT NOT NULL DEFAULT 0
        CHECK (page_views >= 0),
    estimated_unique_visitors BIGINT NOT NULL DEFAULT 0
        CHECK (estimated_unique_visitors >= 0),
    tool_executions BIGINT NOT NULL DEFAULT 0
        CHECK (tool_executions >= 0),
    tool_errors BIGINT NOT NULL DEFAULT 0
        CHECK (tool_errors >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── public.support_queries ─────────────────────────────────────────────────
CREATE TABLE public.support_queries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    name TEXT,
    email TEXT,
    category TEXT,
    subject TEXT,
    message TEXT,
    page_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    priority TEXT NOT NULL DEFAULT 'normal',
    admin_notes TEXT,
    resolved_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── public.security_events ─────────────────────────────────────────────────
CREATE TABLE public.security_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type TEXT NOT NULL CHECK (
        event_type IN (
            'rate_limited',
            'invalid_payload',
            'auth_failure',
            'suspected_abuse'
        )
    ),
    ip_hmac CHAR(64)
        CHECK (
            ip_hmac IS NULL
            OR ip_hmac ~ '^[0-9a-f]{64}$'
        ),
    route_name TEXT
        CHECK (
            route_name IS NULL
            OR char_length(route_name) <= 120
        ),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (
            jsonb_typeof(details) = 'object'
            AND pg_column_size(details) <= 2048
        )
);

-- ── private.audit_log ───────────────────────────────────────────────────────
CREATE TABLE private.audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    table_schema TEXT NOT NULL,
    table_name TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    row_pk TEXT,
    changed_by TEXT,
    before_data JSONB,
    after_data JSONB
);

-- ── private.is_admin() ─────────────────────────────────────────────────────
-- Mirrors the project's existing authorization function. Reads
-- auth.uid() (set via the request.jwt.claim.sub GUC) and checks
-- public.admin_profiles.
--
-- SECURITY DEFINER + search_path = '' (empty) is the recommended
-- hardening for privileged functions. All referenced objects are
-- schema-qualified so the function cannot be hijacked by an attacker
-- who creates a malicious object in their search_path.
CREATE OR REPLACE FUNCTION private.is_admin()
RETURNS boolean
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.admin_profiles
        WHERE user_id = auth.uid()
    )
$$;

REVOKE EXECUTE ON FUNCTION private.is_admin() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION private.is_admin() TO service_role, authenticated;

-- ── private.capture_audit_change() ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION private.capture_audit_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    INSERT INTO private.audit_log
        (table_schema, table_name, operation, row_pk, changed_by,
         before_data, after_data)
    VALUES
        (TG_TABLE_SCHEMA::text, TG_TABLE_NAME::text, TG_OP,
         COALESCE((CASE WHEN TG_OP = 'DELETE' THEN row_to_json(OLD) ELSE row_to_json(NEW) END)->>'id', ''),
         current_user,
         CASE WHEN TG_OP IN ('UPDATE','DELETE') THEN row_to_json(OLD)::jsonb END,
         CASE WHEN TG_OP IN ('INSERT','UPDATE') THEN row_to_json(NEW)::jsonb END);
    RETURN COALESCE(NEW, OLD);
END;
$$;

REVOKE EXECUTE ON FUNCTION private.capture_audit_change() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION private.capture_audit_change() TO service_role;

-- ── Audit triggers ──────────────────────────────────────────────────────────
CREATE TRIGGER audit_support_queries
    AFTER INSERT OR UPDATE OR DELETE ON public.support_queries
    FOR EACH ROW EXECUTE FUNCTION private.capture_audit_change();

CREATE TRIGGER audit_admin_profiles
    AFTER INSERT OR UPDATE OR DELETE ON public.admin_profiles
    FOR EACH ROW EXECUTE FUNCTION private.capture_audit_change();
