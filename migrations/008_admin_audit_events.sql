-- Admin action auditing: allow public.security_events to store admin
-- actions (login, logout, queue retry, support updates) alongside the
-- existing abuse/security event types.
--
-- Safe to run more than once. Run in the Supabase SQL Editor.
-- Required by services/audit.py + delivery/worker.py (_handle_admin_audit).

ALTER TABLE public.security_events DROP CONSTRAINT IF EXISTS security_events_event_type_check;

ALTER TABLE public.security_events ADD CONSTRAINT security_events_event_type_check CHECK (
    event_type IN (
        'rate_limited',
        'invalid_payload',
        'auth_failure',
        'suspected_abuse',
        'admin_action'
    )
);

-- Index for the common "what did admins do lately" query.
CREATE INDEX IF NOT EXISTS security_events_admin_actions_idx
    ON public.security_events (occurred_at DESC)
    WHERE event_type = 'admin_action';
