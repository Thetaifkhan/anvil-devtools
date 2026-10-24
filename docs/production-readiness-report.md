# Production readiness report

## Status: READY FOR STAGING. NOT READY FOR PRODUCTION.

Staging can start once staging environment variables and a staging Supabase
project exist (see `docs/deployment.md`). Production is blocked. The blockers
below are not mitigated by the passing tests.

## Evidence

Exact commands and results are in `docs/test-evidence.md`. Summary:

| Check | Command | Result | Database |
|---|---|---|---|
| Full suite | `PG_PORT=55432 python -m pytest tests/ -q` | 600 passed, 1 skipped (Playwright) | Real PostgreSQL 16 for migration, RLS, and upsert tests |
| Analytics upsert | `pytest tests/test_analytics_upsert_pg.py` | 14 passed | Real PostgreSQL 16 |
| RLS as ordinary roles | `pytest tests/test_rls_access.py` | 12 passed | Real PostgreSQL 16 |
| Migrations | `pytest tests/test_migrations_e2e.py` | 15 passed | Real PostgreSQL 16 |
| Dependency audit | `pip_audit -r requirements-dev.txt` | No known vulnerabilities | n/a |
| JavaScript | `node --test tests/js/*.test.cjs` | 22 passed | n/a |

The database checks cover a fresh schema and the 004→006 upgrade. They run
against a **stubbed** Supabase schema (`000_base_schema.sql` reproduces the
existing tables and roles). They do not prove the live project matches.

## Requirements status

### Database and migrations

- **Verified:** migrations 001–006 apply in order from a clean database and from
  the 004 state. 005 and 006 are idempotent. Analytics upserts are correct on
  PostgreSQL 16 (one row per logical key, normalized slugs, concurrency-safe).
- **Not verified:** the live project's schema, RLS state, grants, and policies.
  Migration 005 must be diffed against the live catalog before it is applied.
- **Not verified:** the Supabase-managed `auth` schema and role definitions. The
  test database stubs them.

### Support submission durability

- **Current state:** submissions are validated, then written to a SQLite outbox,
  then delivered to Supabase by an in-process worker. Claims are exclusive and
  leased. Retries use capped exponential backoff with jitter. Permanent errors
  are dead-lettered. Authentication errors are retried.
- **Not met:** durability across instance replacement. The outbox lives on
  ephemeral storage on Render unless a persistent disk is mounted. A submission
  accepted into that outbox can be lost. The UI reports `queued_volatile` in this
  case, so the user is not misled, but the requirement "do not lose submissions"
  is not met.
- **Required before production:** a persistent disk with `OUTBOX_DURABLE=1`, or an
  external durable queue. Either needs an owner decision and cost approval.

### Worker topology

- **Fixed:** the shipped `render.yaml` ran no delivery at all. It now runs the
  worker on the single web instance.
- **Constraint:** exactly one web instance. Scaling out duplicates the in-process
  worker, and the outbox is not shared.

### Security

- **Verified:** TLS verification, admin authorization, CSRF on admin writes,
  per-client rate limiting, secure session cookie, RLS and grants on PostgreSQL,
  no secrets in the repository, clean dependency audit.
- **Open:** the CSP still requires `'unsafe-inline'` for 36 inline script blocks,
  and advertising and analytics origins remain. See `docs/security-review.md`.
- **Open:** the database role used by the application bypasses RLS. Confirm the
  role in production before relying on RLS for application reads.

### Backups and recovery

- **Status: NOT VERIFIED.** No backup has been taken, inspected, or restored. The
  procedure is in `docs/backup-and-restore.md`. Production cannot proceed until
  an owner completes a restore rehearsal into a scratch database and records the
  result.

### Analytics and privacy

- **Verified in code:** no raw IP storage, HMAC-keyed identifiers, bounded
  payloads, analytics failures do not affect tools, limiter keys never persisted.
- **Open:** no scheduled retention deletion for `analytics_events` and
  `visitor_sessions`. Pruning is off by default. No jurisdiction-specific privacy
  review has been done, and no legal compliance is claimed.

### Render

- **Verified locally:** the start command under gunicorn with two workers starts a
  worker that processes queued items. No `--preload`.
- **Not verified:** the live Render service, its health checks, its environment
  variables, or the secret-file mount for the CA.

### Supabase

- **Not verified:** any live project state. The connection string, pooler host,
  CA certificate, and RLS state must be checked by the owner.

## Blockers for production (priority order)

1. Verified backup and completed restore rehearsal (`docs/backup-and-restore.md`).
2. Durable queue: persistent disk with `OUTBOX_DURABLE=1`, or an external durable
   queue. Owner decision and cost approval.
3. Live catalog diff against migration 005, approved and applied on staging first.
4. CSP without `'unsafe-inline'`, and an owner decision on advertising origins.
5. Confirm the application's database role and its RLS behaviour.
6. Analytics retention policy decided and implemented, with a privacy review.
7. Staging rehearsal of the staged procedure, including the queue-outage drill.

## Unverified items

- Live Supabase project (schema, RLS, grants, backups, PITR availability).
- Live Render service (health checks, variables, secret files, plan limits).
- Browser behaviour (Playwright not installed; workbench browser tests skipped).
- CI execution on GitHub. The workflow was edited and is syntactically valid, but
  has not run.
- Behaviour under real network failure to Supabase (simulated only, through unit
  and integration tests).

## Known defects

- Durable delivery is not achieved on the current Render setup (see above).
- `analytics_daily` has no writer or reader (see the engineering change report).
- Retention is not enforced.
- CSP is not hardened.

## Staging exit criteria

Staging is ready for the production review when all of these hold:

- Staging Supabase project migrated 001–006, with the diff reviewed.
- Staging Render service running with `SUPABASE_WORKER_ENABLED=1` and the CA
  mounted. A test submission reaches `support_queries`.
- Queue-outage drill: database paused, submissions queue, database resumed,
  submissions drain with no loss and no duplicates.
- Authorization drill: a non-admin token sees no support rows and cannot call the
  upsert through PostgREST.
- Backup restore rehearsal completed and recorded.
