-- Speeds up the admin dashboard queries (last-24-hours counts, traffic and
-- engagement pages). Safe to run more than once. Run in the Supabase SQL Editor.
CREATE INDEX IF NOT EXISTS analytics_events_occurred_at_idx
    ON public.analytics_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS analytics_events_session_id_idx
    ON public.analytics_events (session_id);
CREATE INDEX IF NOT EXISTS visitor_sessions_last_seen_idx
    ON public.visitor_sessions (last_seen_at DESC);
CREATE INDEX IF NOT EXISTS visitor_sessions_started_idx
    ON public.visitor_sessions (started_at DESC);
ANALYZE public.analytics_events;
ANALYZE public.visitor_sessions;
