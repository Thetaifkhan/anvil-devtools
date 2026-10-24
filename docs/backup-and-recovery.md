# Backup and recovery

> **Verification status: NOT VERIFIED.** No backup has been taken, restored, or
> inspected as part of this work. Treat backups as unconfirmed until an operator
> completes the verification steps below on the live project.

This document explains what backups Supabase provides on the free
tier, what is and is not covered, and the additional safeguards the
application implements to compensate.

## Supabase managed backups — what's actually covered

| Plan   | Daily snapshot | PITR (point-in-time recovery) | Logical backup | Retention |
|--------|----------------|-------------------------------|-----------------|-----------|
| Free   | Yes (once/day, UTC) | No                       | No              | 7 days    |
| Pro    | Yes (daily)    | Yes (7-day window)             | No              | 7 days (PITR) + 14 days (snapshots) |
| Team   | Yes (daily)    | Yes (14-day window)            | No              | 14 days (PITR) + 14 days (snapshots) |
| Enterprise | Yes (daily) | Yes (30-day window)         | Negotiated      | Negotiated |

**The project is currently on the free tier.** This means:

- The database is snapshotted once per day, around the same UTC time.
- The retention window is **7 days**. Anything older than 7 days is
  gone permanently.
- There is **no point-in-time recovery** — you can restore to the
  last daily snapshot, but not to "5 minutes ago".
- Pausing the project (inactivity on free tier) does NOT delete data,
  but a manual `DROP DATABASE` or project deletion does, and is
  irreversible.
- Supabase support cannot restore a deleted free-tier project.

**Do not assume backups are enabled beyond the daily snapshot.** Verify
in the Supabase dashboard → *Database → Backups* before relying on
this.

## Recovery point and recovery time objectives

| Objective    | Free tier (current)               | Pro tier (recommended for production) |
|--------------|-----------------------------------|---------------------------------------|
| **RPO**      | Up to 24 hours of data loss        | < 5 minutes (with PITR)               |
| **RTO**      | 5–60 minutes (manual restore)     | 5–30 minutes (manual restore)         |

If 24 hours of data loss is unacceptable (it shouldn't be for
analytics, but it definitely is for contact submissions), implement
the independent backup described below.

## Independent backup destination

Because the Supabase daily snapshot is the only safety net, we
implement a **second** backup that is independent of Supabase:

### Option 1: Logical backup to external storage (recommended)

Run a daily `pg_dump` against the Supabase database and push the
result to object storage that is NOT controlled by Supabase.

```bash
#!/usr/bin/env bash
# Run via Render Cron Job at 03:00 UTC daily.
set -euo pipefail
TS=$(date -u +%Y%m%dT%H%M%SZ)
DUMP_FILE="/tmp/anvil-backup-${TS}.sql.gz"
pg_dump "$DATABASE_URL" \
  --no-owner --no-privileges \
  --format=custom \
  --file=/tmp/anvil-backup-${TS}.dump
gzip /tmp/anvil-backup-${TS}.dump

# Push to S3 / R2 / B2 / GCS via the aws-cli (or rclone).
aws s3 cp /tmp/anvil-backup-${TS}.dump.gz \
  "s3://anvil-backups/${TS}.dump.gz"

# Cleanup: keep last 30 days locally.
find /tmp -name 'anvil-backup-*.dump.gz' -mtime +30 -delete
```

Schedule via Render Cron Job (or GitHub Actions, or systemd timer).

### Option 2: Continuous replication to a standby

For real-time backup, set up Postgres logical replication to a
standby database (e.g. another Supabase project, Neon, RDS, or a
self-hosted Postgres). The application then has a live fallback if
the primary is unreachable. This is significantly more complex and
typically only justified for production-grade deployments.

### Option 3: Audit log as a write-ahead log

The `private.audit_log` table records every INSERT, UPDATE, and
DELETE on `support_queries` and `admin_profiles`. The audit log
captures before/after snapshots as JSONB. **It is NOT a substitute
for a backup**:

- It only covers audited tables (not analytics_events or
  visitor_sessions).
- A `DROP TABLE` or `DROP DATABASE` removes the audit log too.
- A privileged attacker with the service role can `DELETE FROM
  private.audit_log` directly.

But it is useful for forensic reconstruction if you have a copy of it
outside the database. Configure the pg_dump script to include
`private.audit_log`.

## Encryption and access controls

- The Supabase database is encrypted at rest (AES-256) on all plans.
- The backup dump file (`.dump.gz`) is **not** encrypted by pg_dump.
  Encrypt it before pushing to object storage:

  ```bash
  gpg --batch --yes --passphrase "$BACKUP_GPG_PASSPHRASE" \
    --symmetric --cipher-algo AES256 \
    --output /tmp/anvil-backup-${TS}.dump.gz.gpg \
    /tmp/anvil-backup-${TS}.dump.gz
  ```

- Object storage (S3, R2, GCS) should have default encryption enabled
  (SSE-S3 or SSE-KMS). The GPG layer above is defense-in-depth.
- The backup bucket must NOT be public. Access is granted only to the
  IAM role used by the backup job.
- The `BACKUP_GPG_PASSPHRASE` and the object-storage credentials are
  stored in Render's secret store, never in `.env` or git.

## Restoration procedure

### Restore from a Supabase snapshot

1. Open the Supabase dashboard → *Database → Backups*.
2. Pick the snapshot you want to restore to.
3. Click "Restore".
4. The database is recreated from the snapshot. The connection string
   stays the same.
5. Restart the Flask app on Render.
6. The worker will resume processing pending outbox items against the
   restored database.

**Caveat:** The snapshot is up to 24 hours old. Any contact
submissions received in the last 24 hours are in the LOCAL OUTBOX
on the Render instance, not in the database. If the instance was
also replaced, those submissions are lost.

### Restore from an independent pg_dump

1. Provision a fresh Supabase project (or use an existing one as
   the restore target).
2. Set the new `DATABASE_URL` in Render's environment.
3. Restore the dump:

   ```bash
   psql "$DATABASE_URL" -f anvil-backup-20251024T030000Z.sql
   ```

4. Restart the Flask app.
5. The worker resumes pending outbox items against the restored
   database.

### Restore into staging (recommended dry-run)

Before restoring to production, ALWAYS test the restore into a
separate staging project:

1. Create a new Supabase project (e.g. `anvil-staging`).
2. Restore the dump into staging:

   ```bash
   psql "$STAGING_DATABASE_URL" -f anvil-backup-20251024T030000Z.sql
   ```

3. Spot-check the restored data:

   ```sql
   SELECT count(*) FROM public.support_queries;
   SELECT count(*) FROM public.analytics_events;
   SELECT count(*) FROM private.audit_log;
   SELECT max(occurred_at) FROM public.analytics_events;
   ```

4. If everything looks right, repeat the procedure against
   production.

## What the application does NOT do

The application deliberately does NOT:

- **Auto-delete old analytics data.** The owner prefers long-term
  retention. See `docs/analytics-and-privacy.md#retention` for the
  suggested manual retention SQL.
- **Use the audit log as a backup.** Audit logs catch logical
  corruption (admin accidentally deletes a row); they do not catch
  database loss or project deletion.
- **Rely on a database trigger for disaster recovery.** The
  `private.capture_audit_change()` trigger is for compliance and
  forensic analysis, not for restoring lost data.
- **Claim backups are enabled when they aren't.** The operator must
  verify in the Supabase dashboard that daily snapshots are running.

## Verification checklist

Run this checklist monthly:

- [ ] Confirm Supabase dashboard shows a successful snapshot in the
      last 24 hours.
- [ ] Confirm the independent pg_dump job ran successfully (check the
      Render Cron Job logs).
- [ ] Confirm a backup file exists in object storage dated within
      the last 24 hours.
- [ ] Restore the latest backup into staging and verify the row
      counts match production (±1%).
- [ ] Rotate the `BACKUP_GPG_PASSPHRASE` if it has been more than
      90 days.
- [ ] Audit who has access to the backup bucket. Remove any stale
      IAM users.
