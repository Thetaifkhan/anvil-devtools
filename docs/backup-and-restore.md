# Backup and restore

> **Status: NOT VERIFIED.** No backup has been taken, inspected, or restored as
> part of this work. Treat every statement below as a procedure to perform, not
> as evidence that a backup exists.

See also `docs/backup-and-recovery.md`, which describes Supabase plan features.

## What must be recoverable

| Data | Location | Why it matters |
|---|---|---|
| Support submissions | `public.support_queries` | User requests. Losing them loses user data. |
| Admin audit records | `private.audit_log` | Accountability. Database-local, not tamper-evident. |
| Analytics | `public.analytics_events`, `visitor_sessions`, `analytics_daily` | Operational; lower sensitivity. |
| Queue state | `private.delivery_queue` and the SQLite outbox | Pending submissions. The SQLite file is **not** in Postgres backups. |
| Schema, RLS, grants, functions | the migration files plus the live catalog | Restoring data without these is unsafe. |
| Configuration | Render and Supabase dashboards | Not stored in the archive. Record variable names only. |

## RPO and RTO targets (to be confirmed by the owner)

- RPO for Postgres: bounded by the plan's backup frequency. Daily snapshots mean
  up to 24 hours of loss. Point-in-time recovery (PITR), if the plan includes it,
  narrows this. Confirm the plan.
- RPO for the SQLite outbox: **unbounded** on ephemeral storage. Unsent
  submissions can be lost on instance replacement. No backup covers this unless a
  persistent disk is in use and is itself backed up.
- RTO: to be measured during a restore rehearsal.

## Procedure (owner to perform)

1. In the Supabase dashboard, confirm the plan, backup retention, and whether PITR
   is enabled. Record the answers.
2. Take a logical backup into an isolated location:
   `pg_dump --format=custom --no-owner --file=anvil-$(date -u +%Y%m%dT%H%M%SZ).dump "$DATABASE_URL"`
   Use the direct connection with `verify-full`. Store the file encrypted, with
   access restricted.
3. Verify the dump: `pg_restore --list anvil-*.dump` must succeed.
4. Restore into a **scratch** database, never into production:
   `createdb anvil_restore_test && pg_restore --no-owner -d anvil_restore_test anvil-*.dump`
5. Validate the restore:
   - constraint and index counts match the source,
   - `SELECT count(*) FROM public.support_queries;` matches the source,
   - RLS is enabled on the six public tables,
   - `public.upsert_analytics_daily` exists with the expected grants.
6. Record the date, backup size, restore duration (RTO), and any discrepancies
   in `docs/production-readiness-report.md`.

## Restoring to production

Only with explicit approval, after a successful rehearsal, with a fresh backup of
the current state taken first.

## Scheduled backups

`render.yaml` contains a commented-out cron backup example that expects
`BACKUP_S3_BUCKET` and related secrets. It is **not active**, and no script for it
has been verified in this work.
