# Incident response

Start here. Each entry lists the symptom, how to confirm it, and the first safe
action. Never paste secret values into tickets or chat. Never change production
data without an explicit decision recorded in the incident notes.

## Queue backlog growing

- **Confirm:** `/admin/queue` pending count rising. Worker heartbeat stale.
- **Check:** `SUPABASE_WORKER_ENABLED=1`; the web service has exactly one instance.
- **Check:** `DATABASE_URL` set and TLS root CA mounted. Refused TLS shows as
  `no_supabase_configured` or repeated connection errors in logs.
- **Action:** fix configuration. Items drain automatically; no manual retry needed
  for transient errors.

## Dead-lettered submissions

- **Confirm:** dead-letter count above zero on `/admin/queue`, with a reason.
- **Meaning:** a permanent error (for example a schema or constraint error).
  Authentication errors are transient and are not dead-lettered.
- **Action:** fix the cause (grant, schema, payload), then requeue each item from
  the dashboard. Do not requeue blindly.

## Database unreachable

- **Confirm:** `/admin/overview` shows Supabase unreachable. Contact form returns
  the honest "queued" status.
- **Action:** wait; the worker retries with capped backoff. Check the Supabase
  project status. Do not switch to `sslmode=require` to get a connection.

## Suspected admin compromise

1. Revoke the admin user's `admin_profiles` row (through the SQL Editor, recorded).
2. Rotate `SECRET_KEY` on Render. This invalidates all admin sessions.
3. Review `private.audit_log` and `public.security_events` for the period.
4. Rotate `SUPABASE_SERVICE_ROLE_KEY` if any server environment was exposed.

## Secret exposed

1. Rotate the affected secret in its provider dashboard.
2. Update the Render environment variable and redeploy.
3. Check git history and build logs. Remove any committed value and treat the old
   value as public.
4. Record the time, scope, and rotation in the incident notes.

## Unexpected worker or process crash

- The outbox keeps items; leases expire after `SUPABASE_QUEUE_LEASE_SECONDS`, and
  other claims recover them. No manual action is required.
- Confirm the restart in Render logs and that the queue count is decreasing.
