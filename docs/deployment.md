# Deployment

This is the staged procedure. Render-specific setup (services, dashboard
steps) is in `RENDER_DEPLOYMENT.md`. Nothing here has been executed against
live Supabase. Each stage has a gate, and the next stage does not start until
the gate passes.

## Topology

- One Render web service running `gunicorn -w 1 -b 0.0.0.0:$PORT app:app`, with a single instance.
  **No `--preload`.** The delivery worker runs as a thread in each process.
- `SUPABASE_WORKER_ENABLED=1` on the web service, which runs the in-process
  worker. This is set in `render.yaml`. Earlier versions shipped `0`, which
  meant no process delivered queued submissions.
- Durable delivery requires a persistent disk or an external queue. Without
  one, leave `OUTBOX_DURABLE=0`. The contact form then reports
  `queued_volatile` honestly.

## Required environment variables

| Variable | Notes |
|----------|-------|
| `SECRET_KEY` | At least 32 characters. Stable across restarts. |
| `SITE_URL` | Canonical HTTPS origin. Enables `Secure` cookies and HSTS. |
| `DATABASE_URL` | `sslmode=verify-full`. |
| `SUPABASE_SSL_ROOT_CERT` | Path to Supabase's root CA secret file. |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | Admin token verification. |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-only. Never expose it to the browser. |
| `IP_HMAC_SECRET` | At least 16 characters. Pseudonymizes IPs. |
| `TRUSTED_PROXY_COUNT` | `1` on Render. |
| `SUPABASE_QUEUE_PATH` | SQLite outbox path. Persistent only if a disk is mounted. |
| `OUTBOX_DURABLE` | `1` only after confirming the path is on a persistent disk. |
| `SUPABASE_WORKER_ENABLED` | `1` for the in-process worker. |
| `SUPABASE_QUEUE_LEASE_SECONDS` | Default 300. |

See `.env.example` for the complete list.

## Migration order

Apply in this order. Each file is idempotent or guarded, and each starts with
`BEGIN` and ends with `COMMIT`.

1. `001_schema_verification.sql`
2. `002_missing_constraints_and_indexes.sql`
3. `003_retry_queue_idempotency.sql`
4. `004_admin_analytics_support.sql`
5. `005_rls_and_least_privilege.sql`
6. `006_analytics_upsert_normalization.sql`

`000_base_schema.sql` is **test-only** and must never be applied to production.

### Running a migration with psql

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/005_rls_and_least_privilege.sql
```

### Running a migration in the Supabase SQL Editor

Paste the file as is and run it. The files contain no psql meta-commands.
Read the editor's output: the run is successful only if no `ERROR` appears.
Do not assume success from a blank result.

## Staged procedure

1. **Back up.** Confirm a restorable backup exists and has been restored to a
   scratch project. See `docs/backup-and-recovery.md`. Status is currently
   NOT VERIFIED.
2. **Disposable database.** Run `pytest tests/test_migrations_e2e.py
   tests/test_rls_access.py` against a disposable PostgreSQL. Gate: all pass.
3. **Catalog diff.** On a staging copy of the live project, compare RLS state,
   policies, and grants (`\dp`, `pg_policies`, `pg_class.relrowsecurity`) with
   the expected state in migration 005. Record every difference. Gate: a
   reviewed list, with no surprises.
4. **Staging migration.** Apply 001–005 to staging. Re-run the RLS checks.
5. **Queue recovery drill.** On staging, pause or block the database and confirm
   that:
   - contact submissions are accepted and queued,
   - the worker retries with backoff, and
   - items drain after the database returns.
6. **Authorization drill.** Confirm a non-admin token sees no support rows and
   cannot execute the analytics upsert through PostgREST.
7. **Deploy the application** to staging, then to production.
8. **Production migration.** Apply 001–005 only after explicit approval.
   Keep the pre-migration grant snapshot for rollback.
9. **Monitor.** Watch `/admin/queue` for pending count, dead-letter count, and
   worker heartbeat. Investigate any growth.

## Rollback

- Application: redeploy the previous image. The outbox schema is backward
  compatible, because the lease columns are nullable.
- Migration 005: restore grants and policies from the snapshot taken in step 8.
  Emergency RLS removal is a manual, reviewed action. It is never automatic.

## Verification commands

```bash
python -m pytest tests/ -q                   # application suite
PG_PORT=55432 python -m pytest tests/test_migrations_e2e.py tests/test_rls_access.py -q
node --test tests/js/*.test.cjs              # JavaScript suite
python -m pip_audit -r requirements-dev.txt  # dependency audit
curl -s https://<site>/healthz               # liveness only; no config or DB detail
```
