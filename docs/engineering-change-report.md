# Engineering change report

## Baseline

- Archive: `anvil-devtools-hardened.zip` (previous pass). Its project root is
  the inner `anvil/` directory. The previous package had wrapped that directory
  in an extra level, which this pass corrects.
- Runtime: Python 3.12.3, Flask 3.1.3, psycopg 3.2.3, gunicorn 26.2.0.
- Database used for tests: PostgreSQL 16.15 (local, disposable).
- No `anvil-devtools-complete-v4.zip` was supplied. Comparison with V4 was not
  possible, and this report does not claim a comparison.

## Defects confirmed in this pass

### D1. Analytics upsert stored `''` as a separate key (migration 004)

- **Root cause.** `public.upsert_analytics_daily` stored the slug unchanged. The
  unique index keys on `COALESCE(tool_slug, '<site-wide>')`, which leaves `''`
  unmapped, so `''` and `NULL` became two logically identical site-wide rows.
- **Reproduction.** Applied 000–004 to a clean PostgreSQL 16 database, called the
  function with `NULL` then `''`. Result: 2 rows.
- **Fix.** Migration `006_analytics_upsert_normalization.sql`, forward-only. It
  trims and normalizes the slug with `NULLIF(btrim(...), '')`, validates that
  counts are non-null and non-negative, and keeps the conflict target matching
  the index expression.
- **Files.** `migrations/006_analytics_upsert_normalization.sql` (new).
- **Verification.** `tests/test_analytics_upsert_pg.py`: 14 passed on real
  PostgreSQL 16. Covers clean apply, the 004→006 upgrade, idempotent re-apply,
  first insert, repeat-update in place, site-wide vs tool distinctness, `NULL`/`''`/
  whitespace normalization, invalid counts, and eight concurrent sessions
  hammering the same keys (one logical row per key).

### D2. Nothing writes or reads `analytics_daily`

- **Finding.** No application code calls `upsert_analytics_daily`. The admin
  dashboard reads `analytics_events` and `visitor_sessions` directly.
- **Decision.** Not building an aggregation pipeline in this pass. The table and
  the function are retained and tested, so a future aggregator has a correct
  target. The semantics are documented in migration 006: the function sets
  absolute totals, so callers must pass full-day totals. Passing deltas would
  overwrite data.
- **Status.** Documented limitation, not a defect fix.

### D3. Shipped Render configuration delivered nothing

- **Root cause.** `render.yaml` set `SUPABASE_WORKER_ENABLED=0` on the web
  service, and the Background Worker that would deliver was commented out.
- **Why not a separate worker.** The SQLite outbox lives on one service's disk.
  Services do not share a filesystem, so a Background Worker cannot read it.
- **Fix.** `SUPABASE_WORKER_ENABLED=1` on the single web instance, with
  `WEB_CONCURRENCY=1`. Documentation updated in `render.yaml`, `.env.example`,
  `docs/deployment.md`, `docs/environment-variables.md`.
- **Verification.** `test_render_config_runs_delivery_worker_in_process`. Earlier
  gunicorn run with two workers showed a worker processing a queued item.
- **Behaviour change.** Delivery to Supabase now runs. This requires
  `DATABASE_URL` with the CA configured. Without it, items are requeued safely.

### D4. Committed-looking values and ignore rules

- **Scan.** Searched for JWT-shaped strings, private keys, DSNs with embedded
  passwords, and inline password assignments. The only hits were sample JWT
  strings in the JWT-decoder tool's demo text and its test fixtures. They are
  not credentials.
- **Fix.** `.gitignore` and `.dockerignore` now exclude `.env.*` (except
  `.env.example`), `*.sqlite3*`, `*.log`, coverage output.
- **Verification.** `test_env_example_contains_no_real_secret_values`.

### D5. Vulnerable dependency pin

- `requests==2.32.3` had two known vulnerabilities (PYSEC-2026-1872,
  PYSEC-2026-2275). Pinned to 2.33.0. `pip_audit -r requirements-dev.txt`:
  "No known vulnerabilities found."

### D6. Packaging

- The previous deliverable nested the project under an extra directory. The
  final archive is built with the project root at the top level of the ZIP.

### D7. Documentation accuracy

- `RENDER_DEPLOYMENT.md` said the SQL Editor silently ignores psql meta-commands.
  That claim is unverified and was removed. The meta-commands themselves were
  removed from migrations (see the previous report).
- `docs/environment-variables.md` initially said the app refuses to start without
  `SECRET_KEY`. The code only warns, so the document was corrected.

## Defects from earlier passes, re-verified in this pass

- Migration 005 RLS policies: 12 role-switched tests pass on PostgreSQL 16.
- Admin CSRF enforcement on four write endpoints: 14 tests pass.
- Regression set for TLS, backoff, lease recovery, forked-worker start,
  limiter identity, queue flooding: 41 tests pass.

## Not changed in this pass (remaining)

- **Content Security Policy.** 36 inline `<script>` blocks across 39 templates
  still require `'unsafe-inline'`. Removing it needs a nonce or hash for each
  block. Advertising and analytics origins remain in the policy. Not fixed.
- **Durable queue.** The outbox is SQLite on Render's ephemeral disk unless a
  persistent disk is mounted. Pending submissions can be lost on instance
  replacement. The UI and documentation state this honestly, but durability is
  not achieved. Not fixed. Options are in `docs/supabase-retry-and-queue.md`.
- **Analytics retention and consent.** Retention pruning is disabled by default
  (`SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS=0`), and no scheduled deletion of
  `analytics_events` or `visitor_sessions` exists. Not implemented.
- **Backups.** Not taken, verified, or restored. See `docs/backup-and-restore.md`.
- **Browser tests.** Playwright is not installed. Not run.
- **Live Supabase and live Render.** Not accessed. All live-state claims are
  unverified.

## Breaking changes

1. Connections with `sslmode=require` (or unset `sslmode` in a DSN that
   explicitly sets a weaker mode) are refused. Migrate to `verify-full` and mount
   the root CA.
2. `render.yaml` now runs the delivery worker. Delivery attempts begin against
   Supabase on deploy.
3. Migrations 005 and 006 change live access (005) or function behaviour (006).
   Both need the staged procedure in `docs/deployment.md`.

## Migration considerations

- 006 is forward-only and changes no stored data.
- 005 changes privileges. Capture grants before applying (see
  `docs/database-migrations.md`).
