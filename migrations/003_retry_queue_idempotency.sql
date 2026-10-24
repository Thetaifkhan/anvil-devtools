-- ─────────────────────────────────────────────────────────────────────────────
-- 003_retry_queue_idempotency.sql
-- ─────────────────────────────────────────────────────────────────────────────
-- Creates the *private* delivery queue table used as a transactional claim
-- pool by the worker. Browsers never have RLS access to this table — it
-- is touched only by the service-role connection.
--
-- This SQL table is NOT the durable fallback queue. The application-level
-- durable outbox lives in the Flask process (see delivery/outbox.py) so
-- the app can keep accepting requests when Supabase itself is unreachable.
-- The Postgres queue here is used as an internal claim pool once the
-- outbox successfully hands off a delivery to the database.
--
-- Apply with EITHER of the following:
--
-- 1. psql (terminal, recommended for scripted deploys):
--      psql "$DATABASE_URL" \
--        -v ON_ERROR_STOP=1 \
--        -f migrations/003_retry_queue_idempotency.sql
--
-- 2. Supabase SQL Editor (Dashboard → SQL → New Query):
--    Paste the file's contents (the ``\set ON_ERROR_STOP on`` line is a
--    psql meta-command and is silently ignored by the SQL Editor; you do
--    NOT need to delete it). Click RUN. The Editor reports each
--    NOTICE/EXCEPTION line-by-line. If any statement fails, fix the
--    issue and re-run the whole file — every block is idempotent.
--
-- Both ``psql`` and the Supabase SQL Editor run inside an implicit
-- transaction by default, so a mid-script failure rolls back. The
-- explicit ``BEGIN;`` / ``COMMIT;`` below is belt-and-braces.
-- ─────────────────────────────────────────────────────────────────────────────


BEGIN;

-- ── delivery_queue ──────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'private' AND table_name = 'delivery_queue'
    ) THEN
        EXECUTE $stmt$
            CREATE TABLE private.delivery_queue (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                event_key UUID NOT NULL UNIQUE,
                event_type TEXT NOT NULL
                    CHECK (event_type IN (
                        'analytics_event',
                        'contact_submission',
                        'admin_audit'
                    )),
                payload JSONB NOT NULL
                    CHECK (jsonb_typeof(payload) = 'object'
                           AND pg_column_size(payload) <= 16384),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                attempt_count INTEGER NOT NULL DEFAULT 0
                    CHECK (attempt_count >= 0),
                last_error_code TEXT
                    CHECK (last_error_code IS NULL
                           OR char_length(last_error_code) <= 120),
                last_error_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,
                dead_lettered_at TIMESTAMPTZ,
                dead_letter_reason TEXT
                    CHECK (dead_letter_reason IS NULL
                           OR char_length(dead_letter_reason) <= 500)
            )
        $stmt$;
        RAISE NOTICE 'Created private.delivery_queue.';
    ELSE
        RAISE NOTICE 'private.delivery_queue already exists.';
    END IF;
END $$;

-- Index used by the worker's `FOR UPDATE SKIP LOCKED` claim query. Partial
-- (only pending rows) so it stays small even when the table grows.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'private' AND tablename = 'delivery_queue'
          AND indexname = 'delivery_queue_pending_idx'
    ) THEN
        EXECUTE $stmt$
            CREATE INDEX delivery_queue_pending_idx
                ON private.delivery_queue (next_attempt_at)
                WHERE delivered_at IS NULL AND dead_lettered_at IS NULL
        $stmt$;
        RAISE NOTICE 'Created delivery_queue_pending_idx.';
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'private' AND tablename = 'delivery_queue'
          AND indexname = 'delivery_queue_dead_letter_idx'
    ) THEN
        CREATE INDEX delivery_queue_dead_letter_idx
            ON private.delivery_queue (dead_lettered_at DESC)
            WHERE dead_lettered_at IS NOT NULL;
        RAISE NOTICE 'Created delivery_queue_dead_letter_idx.';
    END IF;
END $$;

-- ── RLS: enable but no policies for anon/authenticated ──────────────────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables
        WHERE schemaname = 'private' AND tablename = 'delivery_queue'
          AND rowsecurity = true
    ) THEN
        ALTER TABLE private.delivery_queue ENABLE ROW LEVEL SECURITY;
        RAISE NOTICE 'Enabled RLS on private.delivery_queue.';
    END IF;
END $$;

-- No policies are defined. By default Supabase RLS denies everything that is
-- not explicitly granted. The service role bypasses RLS entirely
-- (BYPASSRLS), which is the only role that needs to read or write this
-- table. Browsers, anon users, and authenticated non-admin users therefore
-- cannot touch the queue.

-- Revoke any accidental default privileges from PUBLIC, anon, and authenticated.
-- Split into separate statements so each revocation is independently
-- visible in the migration log.
REVOKE ALL ON private.delivery_queue FROM PUBLIC;
REVOKE ALL ON private.delivery_queue FROM anon;
REVOKE ALL ON private.delivery_queue FROM authenticated;
-- Service role already bypasses RLS; still grant DML so a non-bypass
-- service-role connection can use it.
GRANT SELECT, INSERT, UPDATE, DELETE ON private.delivery_queue TO service_role;

-- Grant on the sequence backing the IDENTITY column.
REVOKE ALL ON SEQUENCE private.delivery_queue_id_seq FROM PUBLIC;
REVOKE ALL ON SEQUENCE private.delivery_queue_id_seq FROM anon;
REVOKE ALL ON SEQUENCE private.delivery_queue_id_seq FROM authenticated;
GRANT USAGE, SELECT ON SEQUENCE private.delivery_queue_id_seq TO service_role;

-- ── Optional helper: a function the worker calls inside a transaction to
--    claim a batch of pending items safely across multiple workers.
--
--    The body uses ``RETURN QUERY UPDATE ... RETURNING ...`` so the
--    updated rows flow back to the caller as the function's result
--    set. A bare ``UPDATE ... RETURNING`` inside a PL/pgSQL function
--    with a ``RETURNS TABLE`` declaration compiles but returns nothing
--    to the caller — the rows are silently discarded.
-- ────────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION private.claim_delivery_queue(
    p_batch_size integer DEFAULT 10
) RETURNS TABLE (
    id bigint,
    event_key uuid,
    event_type text,
    payload jsonb,
    attempt_count integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    claimed_ids bigint[];
BEGIN
    IF p_batch_size < 1 OR p_batch_size > 100 THEN
        RAISE EXCEPTION 'batch_size must be between 1 and 100';
    END IF;

    SELECT array_agg(q.id) INTO claimed_ids
    FROM (
        SELECT private.delivery_queue.id
        FROM private.delivery_queue
        WHERE private.delivery_queue.delivered_at IS NULL
          AND private.delivery_queue.dead_lettered_at IS NULL
          AND private.delivery_queue.next_attempt_at <= now()
        ORDER BY private.delivery_queue.next_attempt_at
        LIMIT p_batch_size
        FOR UPDATE SKIP LOCKED
    ) q;

    IF claimed_ids IS NULL OR array_length(claimed_ids, 1) IS NULL THEN
        -- RETURN QUERY with no rows returns an empty result set, which
        -- is what we want when there are no pending items.
        RETURN;
    END IF;

    -- RETURN QUERY UPDATE ... RETURNING ... returns the updated rows
    -- as the function's result set. This is the correct PL/pgSQL
    -- syntax for an UPDATE whose result is consumed by the caller.
    RETURN QUERY
        UPDATE private.delivery_queue
           SET attempt_count = private.delivery_queue.attempt_count + 1,
               last_error_at = COALESCE(private.delivery_queue.last_error_at, now())
         WHERE private.delivery_queue.id = ANY(claimed_ids)
        RETURNING
            private.delivery_queue.id,
            private.delivery_queue.event_key,
            private.delivery_queue.event_type,
            private.delivery_queue.payload,
            private.delivery_queue.attempt_count;
END;
$$;

REVOKE EXECUTE ON FUNCTION private.claim_delivery_queue(integer) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION private.claim_delivery_queue(integer) TO service_role;

COMMIT;

-- ── Rollback guidance ──────────────────────────────────────────────────────
-- DROP FUNCTION IF EXISTS private.claim_delivery_queue(integer);
-- DROP INDEX IF EXISTS private.delivery_queue_pending_idx;
-- DROP INDEX IF EXISTS private.delivery_queue_dead_letter_idx;
-- DROP TABLE IF EXISTS private.delivery_queue;
