# Render deployment instructions

This guide deploys the Anvil DevTools Flask app on Render with the
Supabase integration, durable queue, and admin dashboard fully
configured.

## Prerequisites

1. A Render account.
2. A Supabase project (free tier is sufficient to start).
3. The Supabase database password (set when the project was created).
4. The Supabase project URL, anon key, and service-role key (from
   *Project Settings → API*).
5. A high-entropy secret for `IP_HMAC_SECRET` (≥ 16 chars; 32 chars
   recommended).
6. A high-entropy secret for `SECRET_KEY` (≥ 32 chars).
7. (Optional) An S3-compatible bucket and credentials for off-site
   backups.

## Step 1 — Apply the database migrations (MANUAL — never automatic)

The migrations are NOT applied automatically by the Flask app. You
must apply them manually against your Supabase database BEFORE the
first deploy, in this order. Each file is idempotent so re-running
after a partial failure is safe.

Two equally-valid ways to apply the migrations:

### Option A — `psql` (terminal, recommended for scripted deploys)

```bash
# 1. Schema verification — fails loud if the original schema is missing.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/001_schema_verification.sql

# 2. Missing constraints and indexes — safe to re-run.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/002_missing_constraints_and_indexes.sql

# 3. Retry queue + idempotency table.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/003_retry_queue_idempotency.sql

# 4. Admin analytics support columns.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f migrations/004_admin_analytics_support.sql
```

### Option B — Supabase SQL Editor (Dashboard)

1. Open the Supabase dashboard for your project.
2. Click *SQL Editor* in the left nav → *New Query*.
3. Open the migration file from this repo (e.g. `migrations/001_schema_verification.sql`).
4. Select all → copy → paste into the SQL Editor.
5. Click *RUN*. Migrations contain no psql meta-commands, so the SQL
   Editor runs them as written. The `psql` runner passes
   `-v ON_ERROR_STOP=1` on the command line instead (see Option A).
6. The Editor reports each `NOTICE` / `EXCEPTION` line-by-line. If
   any statement fails, fix the issue and re-run the WHOLE file —
   every block is idempotent so partial success is fine.
7. Repeat for files 002, 003, 004 in order.

If step 1 fails, your database is missing one of the tables the spec
requires (`admin_profiles`, `visitor_sessions`, `analytics_events`,
`analytics_daily`, `support_queries`, `security_events`, or the
private audit infrastructure). Apply the project's original schema
migration first, then re-run step 1.

`DATABASE_URL` for `psql` should look like:

```
postgresql://postgres:YOUR_URL_ENCODED_PASSWORD@db.iiaalcfzqqkzxcnmnedt.supabase.co:5432/postgres?sslmode=verify-full
```

If your password contains `@`, `:`, `/`, `?`, or `#`, URL-encode it
(e.g. `p@ss` becomes `p%40ss`).

## Step 2 — Provision the Render web service

### Option A: Use `render.yaml` (recommended)

1. Push this repository to GitHub.
2. In Render, go to *Blueprints* → *New Blueprint Instance*.
3. Pick the repository. Render reads `render.yaml` and provisions
   the services defined there.
4. Review the plan Render shows, then click *Apply*.

The `render.yaml` in this repo defines:

- A web service running the Flask app on the Docker image.
- Environment variables (most are left as placeholders — Render
  prompts you to fill them in).

### Option B: Manual configuration

1. In Render, create a new *Web Service*.
2. Connect the GitHub repository.
3. Set the runtime to *Python 3*.
4. Build command: `pip install -r requirements.txt`
5. Start command: `gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app:app`
   Do NOT add `--preload`. The delivery worker is a thread, and threads
   do not survive `fork()`. With `--preload` the thread started in the
   master is lost in the worker, so delivery silently stops. Without
   preload, each worker process starts its own worker thread. `-w 1` is
   the recommended default; see Step 4.
6. Set the health check path to `/healthz`.
7. Add the environment variables listed below.

## Step 3 — Set environment variables

In Render's *Environment* tab, set the following. Variables marked
**required** must be set or the deploy will fail; **optional**
variables have sensible defaults.

### Required

| Variable                     | Example                                            | Purpose |
|------------------------------|----------------------------------------------------|---------|
| `SECRET_KEY`                 | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | Signs Flask session cookies (admin auth). |
| `SITE_URL`                   | `https://anvil-devtools.onrender.com`              | Used in canonical URLs, sitemap, HSTS. |
| `DATABASE_URL`               | `postgresql://postgres:PASS@db.iiaalcfzqqkzxcnmnedt.supabase.co:5432/postgres?sslmode=verify-full` | Direct Postgres connection. Must use `verify-full`. `require`, `prefer`, `allow` and `disable` are refused because they do not verify the server certificate. |
| `SUPABASE_SSL_ROOT_CERT`     | `/etc/secrets/supabase-ca.crt`                     | Path to Supabase's root CA (download from Project Settings → Database → SSL). Upload it as a Render *Secret File*. Without it, verify-full uses system CAs and Supabase's certificate will usually fail verification. |
| `SUPABASE_URL`               | `https://iiaalcfzqqkzxcnmnedt.supabase.co`         | Auth REST API base URL. |
| `SUPABASE_ANON_KEY`          | `eyJhbGciOi...`                                    | Used to verify admin JWTs against the auth API. |
| `SUPABASE_SERVICE_ROLE_KEY`  | `eyJhbGciOi...`                                    | Server-only; used for queue delivery, audit reads. NEVER expose. |
| `IP_HMAC_SECRET`             | `python -c "import secrets; print(secrets.token_urlsafe(32))"` | HMAC key for pseudonymizing IPs. |

### Optional (with defaults)

| Variable                                | Default | Purpose |
|-----------------------------------------|---------|---------|
| `PORT`                                  | `8080`  | Listening port (Render sets this). |
| `WEB_CONCURRENCY`                       | `1`     | Number of gunicorn workers. Default 1 so the in-process delivery worker (when enabled) is single-instance. |
| `SUPABASE_CONNECT_TIMEOUT`              | `5`     | Seconds; bounded so an unresponsive DB never pins a worker. |
| `SUPABASE_STATEMENT_TIMEOUT`            | `10`    | Seconds; per-statement timeout at the server. |
| `SUPABASE_RETRY_BASE_DELAY_SECONDS`     | `2`     | Initial mean backoff for transient failures. |
| `SUPABASE_RETRY_MAX_DELAY_SECONDS`      | `900`   | 15-minute cap on a single backoff. |
| `SUPABASE_QUEUE_PATH`                   | `/tmp/anvil-outbox.sqlite3` | SQLite path. **Set OUTBOX_DURABLE=1 ONLY if this is on a persistent disk.** |
| `OUTBOX_DURABLE`                         | `0`     | Honesty flag. `0` = best-effort (submissions lost on instance replacement); `1` = durable (operator verified the path is on a persistent volume). |
| `SUPABASE_WORKER_ENABLED`               | `0`     | Set `1` to run the delivery worker thread inside the web service (single-instance only). |
| `SUPABASE_WORKER_POLL_SECONDS`          | `5`     | Worker poll interval. |
| `SUPABASE_MAX_QUEUE_ITEMS`              | `10000` | Outbox capacity. |
| `SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS` | `0`     | Old analytics event cutoff. Default 0 = pruning DISABLED (long-term retention preferred). Set to a positive integer to enable pruning. Contact submissions are NEVER pruned regardless of this setting. |
| `SUPABASE_QUEUE_SATURATION_ALERT_PCT`  | `80`    | When pending items reach this percentage of `SUPABASE_MAX_QUEUE_ITEMS`, the worker logs a WARNING every `SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN` minutes and the admin dashboard surfaces a saturation alert. Set to 0 to disable. |
| `SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN` | `15` | Minimum minutes between saturation log entries (prevents log spam). |
| `FLASK_ENV`                             | `production` | |
| `FLASK_DEBUG`                           | `0`     | |

## Step 4 — Configure the worker

Two topologies. Pick ONE — never both, otherwise two worker processes
will compete on the same SQLite outbox file.

**Critical fact:** Two separate Render services do NOT share a
filesystem. The web service's `/tmp/anvil-outbox.sqlite3` is invisible
to the Background Worker, and vice versa. Setting `SUPABASE_QUEUE_PATH`
to the same value on both services does NOT make them share the queue
file — each gets its own copy at that path.

### Option A: In-process worker on the web service (simplest durable)

Use this when you have a single web service and want the worker to run
in the same process. Requires a persistent disk for durability.

1. Render web service on a paid plan (Starter or higher) that supports
   persistent disks.
2. Mount a persistent disk at `/var/lib/anvil`.
3. Set the following env vars on the web service:
   - `SUPABASE_QUEUE_PATH=/var/lib/anvil/outbox.sqlite3`
   - `OUTBOX_DURABLE=1` (only after verifying the disk survives restart)
   - `SUPABASE_WORKER_ENABLED=1` (in-process worker thread)
   - `WEB_CONCURRENCY=1` (single gunicorn worker — see below)
4. Restart the web service.

**Verify the disk is actually persistent** before setting
`OUTBOX_DURABLE=1`:
```bash
# On the Render shell (Dashboard → Shell):
echo "test" > /var/lib/anvil/persistence-test
# Restart the service via the Render dashboard, then:
cat /var/lib/anvil/persistence-test  # must print "test"
```

If the file is gone after restart, the disk is NOT actually persistent
and `OUTBOX_DURABLE` must remain `0`.

**Why `WEB_CONCURRENCY=1`:** Multiple gunicorn workers each fork a
child process, and each child would start its own worker thread
competing on the same SQLite file. SQLite WAL locking handles this
safely but wastes resources. For higher throughput, scale
horizontally (more containers, each with its own disk — see Option B).

### Option B: Separate Background Worker + external durable queue

Use this when you want the web service and the worker to scale
independently. Requires an external durable queue (Redis, SQS, Cloud
Tasks) because the two services cannot share a SQLite file.

1. Provision a Render *Background Worker* service using the same Docker
   image (Background Workers require a paid Render plan).
2. Provision an external durable queue (Redis, SQS, etc.).
3. Replace the SQLite outbox implementation
   (`delivery/outbox.py`) with one that uses the external queue. The
   interface is small: `enqueue`, `claim_pending`, `mark_delivered`,
   `mark_failed`, `dead_letter`, `requeue_dead_letter`, `counts`,
   `list_recent`, `list_dead_lettered`.
4. Both web service and Background Worker connect to the SAME external
   queue URL.
5. Set `SUPABASE_WORKER_ENABLED=0` on the web service.
6. Background Worker start command:
   ```
   python -m delivery.worker_cli --batch 10 --interval 5
   ```

See `docs/supabase-retry-and-queue.md` for the full architecture
diagrams and the upgrade path.

### Option C: Web service on free tier, in-process worker (best-effort only)

Use this when you cannot pay for a persistent disk. The application
is honest about the limitation.

1. Render web service on the free tier.
2. Set the following env vars on the web service:
   - `SUPABASE_QUEUE_PATH=/tmp/anvil-outbox.sqlite3` (ephemeral)
   - `OUTBOX_DURABLE=0` (the default — best-effort)
   - `SUPABASE_WORKER_ENABLED=1` (in-process worker)
   - `WEB_CONCURRENCY=1`
3. Restart the web service.

In this mode:
- The contact form honestly tells users the submission is "queued
  locally (best-effort)".
- The admin dashboard shows a red "best-effort" warning.
- Pending contact submissions are LOST on instance replacement (Render
  free tier recycles instances after 15 minutes of inactivity).
- The operator should drain the queue (`python -m
  delivery.worker_cli --once` repeatedly) before any deploy.

## Step 5 — Configure backups

1. Provision an S3-compatible bucket (AWS S3, Cloudflare R2, Backblaze
   B2).
2. Add a Render *Cron Job* service:
   - Schedule: `0 3 * * *` (daily at 03:00 UTC).
   - Build command: `pip install -r requirements.txt awscli`
   - Start command:
     ```bash
     ./scripts/backup-database.sh
     ```
   (You'll need to create `scripts/backup-database.sh` based on the
   template in `docs/backup-and-recovery.md`.)
3. Add environment variables for the backup:
   - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (or use IAM role).
   - `BACKUP_S3_BUCKET`.
   - `BACKUP_GPG_PASSPHRASE` (32+ chars).

See `docs/backup-and-recovery.md` for the full backup strategy.

## Step 6 — Verify the deploy

1. Visit the deployed URL. The home page should load.
2. Visit `/contact`. Submit a test bug report. Note the reference code.
3. Visit `/admin/login`. You should see the login form.
4. To actually log in, paste a Supabase access token for a user that
   exists in `public.admin_profiles`:
   - In the Supabase dashboard → *Auth → Users*, find your admin user.
   - Use the *Get user token* feature (or sign in via a test page
     and copy the access token from the URL fragment).
   - Paste the token into `/admin/login`.
5. Once logged in, verify the dashboard sections load. The Overview
   page should show the Supabase connectivity pill as "online".
6. Visit `/admin/queue`. You should see the contact submission you
   sent in step 2 in the "delivered" list (or "pending" if Supabase
   was briefly unavailable).

## Step 7 — Configure the admin user

To add yourself as an admin:

```sql
-- Run against your Supabase database.
INSERT INTO public.admin_profiles (user_id, display_name)
VALUES ('YOUR-AUTH-USER-UUID', 'Your Name')
ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name;
```

`YOUR-AUTH-UUID` is the `id` (a UUID) of your user in
`auth.users`, visible in the Supabase dashboard's *Auth → Users* page.

## Durability limitations on Render free tier

⚠️ **Read this before going live.**

On Render free tier without a persistent disk:

- The SQLite outbox at `/tmp/anvil-outbox.sqlite3` is **ephemeral**.
- It survives gunicorn worker restarts within the same instance.
- It does NOT survive instance replacement (Render free tier recycles
  instances after inactivity or after deploys).
- Pending contact submissions in the outbox are **lost** when the
  instance is replaced.

Mitigations, in order of preference:

1. **Upgrade to a paid plan** and attach a persistent disk at
   `/var/lib/anvil`, then set
   `SUPABASE_QUEUE_PATH=/var/lib/anvil/outbox.sqlite3`.
2. Run the worker frequently (`SUPABASE_WORKER_POLL_SECONDS=2`) so
   the queue drains quickly and the chance of pending items at the
   moment of instance replacement is small.
3. Replace the SQLite outbox with an external durable queue (Redis,
   SQS). See `docs/supabase-retry-and-queue.md` for the upgrade path.

## Rollback

To roll back a deploy:

1. In Render, go to the web service → *Deploys*.
2. Pick the previous deploy and click *Roll back to this deploy*.
3. Render redeploys the previous commit.

Database migrations are NOT automatically rolled back. If a migration
broke something, follow the rollback guidance in the migration file
itself (each migration has a "Rollback guidance" comment at the
bottom).

## Troubleshooting

### "Supabase unreachable" pill in the admin dashboard

- Verify `DATABASE_URL` is set correctly in Render.
- Verify the Supabase project is not paused (Supabase dashboard →
  *Project Settings → Pause project*).
- Check the gunicorn logs for connection errors.
- Try `psql "$DATABASE_URL" -c 'SELECT 1'` from your local machine
  with the same `DATABASE_URL`. If that fails, the credentials or
  network are wrong.

### Admin login fails with "could not verify that access token"

- Verify `SUPABASE_URL` and `SUPABASE_ANON_KEY` are set in Render.
- Verify the token you pasted is for the correct Supabase project.
- Verify the token hasn't expired (Supabase access tokens expire
  after 1 hour by default).

### Admin login succeeds but routes redirect back to /admin/login

- This means `is_user_admin(user_id)` returned False.
- Verify your user is in `public.admin_profiles`.
- Check the gunicorn logs for "Denied admin access for user_id=...".
- The admin lookup caches the result for 60 seconds. Restart the app
  to clear the cache.

### Queue items are stuck in "pending"

- Verify the worker is running: `SUPABASE_WORKER_ENABLED=1` on the
  web service, OR a separate Background Worker is provisioned.
- Check `/admin/queue` for the dead-letter count.
- Run `python -m delivery.worker_cli --once --batch 50` to process a
  batch manually.
- Inspect the last_error_msg column for the stuck items.
