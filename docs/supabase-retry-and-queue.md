# Supabase retry, durable outbox, and queue

This document explains how the application keeps accepting user input
when Supabase is paused, restarting, or otherwise unreachable, and how
the durable queue reconciles once connectivity returns.

## Architecture overview

```
┌─────────────┐     ┌────────────────────────┐     ┌──────────────┐
│   Browser   │────▶│  Flask /api/* route    │────▶│ Local outbox │
│ (analytics, │     │  (validates + queues)  │     │  (SQLite,    │
│  contact)   │     └────────────────────────┘     │  WAL mode)  │
└─────────────┘              │                    └──────┬───────┘
                             │                           │
                             ▼                           ▼
                    ┌─────────────────┐       ┌────────────────────┐
                    │ HTTP 202 back  │       │ Background worker  │
                    │ to the browser │       │  (delivery thread) │
                    └─────────────────┘       └─────────┬──────────┘
                                                        │
                                                        ▼
                                              ┌──────────────────┐
                                              │  Supabase DB    │
                                              │  (with RLS,     │
                                              │  audit triggers,│
                                              │  delivery_queue)│
                                              └──────────────────┘
```

## Why a local outbox at all?

Supabase's free tier pauses the database after a period of inactivity.
The first request after a pause can take 30+ seconds while the
database warms up. The browser will not wait that long. The local
outbox lets us:

1. **Return 202 immediately** with a reference code, so the user is
   never blocked on Supabase.
2. **Survive process restarts** — the SQLite file is opened in WAL
   mode with a 5-second busy_timeout, so concurrent writes from the
   Flask app and the worker don't conflict.
3. **Retry with exponential backoff** — failed deliveries are
   rescheduled with `delay = uniform(0, min(max_delay, base * 2^attempt))`
   and full jitter (see `delivery/errors.py:compute_backoff`).

## What the outbox stores

Each row in the SQLite `outbox` table has:

- `event_key` (UUID, UNIQUE) — the idempotency key. Duplicate
  submissions with the same key are no-ops, so retries of the same
  logical event never create duplicate database rows.
- `event_type` — one of `analytics_event`, `contact_submission`,
  `admin_audit`.
- `payload` (JSON) — the full event payload, capped at a reasonable
  size. For analytics events this is ~4 KB; for contact submissions
  it includes the message body (≤ 8 KB).
- `created_at`, `next_attempt_at`, `attempt_count`,
  `last_error_code`, `last_error_msg`,
  `delivered_at`, `dead_lettered_at`, `dead_letter_reason`.

The state machine is:

```
pending ──claim──▶ in_flight ──success──▶ delivered
                      │
                      └──transient error──▶ retrying (next_attempt_at in future)
                                                │
                                                └──permanent error──▶ dead_lettered
                                                                        │
                                                                        └──admin retry──▶ pending
```

## Idempotency guarantees

1. **Stable event_key.** The browser generates a UUID per form
   submission; if the network fails and the user clicks "submit"
   again, the same key is reused. On the server, the contact route
   also accepts a client-supplied `idempotency_key` for the same
   purpose.
2. **Outbox-level dedup.** `INSERT INTO outbox (event_key, ...)`
   enforces `UNIQUE(event_key)`; a duplicate insert is a no-op and
   returns the existing row.
3. **Database-level dedup.** Each handler does
   `INSERT ... ON CONFLICT DO NOTHING` (for analytics events) or
   `INSERT ... ON CONFLICT (reference_code) DO NOTHING` (for contact
   submissions). Even if the worker delivers the same event twice,
   the database ends up with one row.
4. **Private delivery_queue.** Before delivering to the public table,
   the worker upserts into `private.delivery_queue` keyed by
   `event_key`. This is the secondary defence against duplicates and
   is also the multi-worker coordination point.

## Retry policy

| Setting                          | Default | Env var                                  |
|----------------------------------|---------|------------------------------------------|
| Initial backoff                  | 2 s     | `SUPABASE_RETRY_BASE_DELAY_SECONDS`      |
| Maximum backoff                  | 900 s   | `SUPABASE_RETRY_MAX_DELAY_SECONDS`       |
| Connect timeout                  | 5 s     | `SUPABASE_CONNECT_TIMEOUT`               |
| Statement timeout                | 10 s    | `SUPABASE_STATEMENT_TIMEOUT`             |
| Worker poll interval             | 5 s     | `SUPABASE_WORKER_POLL_SECONDS`           |
| Max items in queue               | 10,000  | `SUPABASE_MAX_QUEUE_ITEMS`               |
| Old analytics event age cutoff   | 30 days | `SUPABASE_QUEUE_MAX_AGE_DAYS`            |

The backoff is **full jitter**: `delay = uniform(0, min(max_delay, base * 2^attempt))`
with a 1 ms minimum floor to prevent tight retry loops.

`Retry-After` HTTP headers (from 429 responses) are honoured as the
upper bound of the jitter.

## Error classification

The classifier in `delivery/errors.py:classify_exception` divides
exceptions into transient and permanent:

**Transient** (retry with backoff):

- Connection refused / timeout / DNS / TLS reset (`OSError`,
  `TimeoutError`, `ConnectionError`)
- Postgres "the database system is starting up" (`57P03`)
- Statement timeout (`57014`, but treated as transient here)
- Serialization / deadlock (`40001`, `40P01`)
- Admin shutdown during a deploy (`57P02`)
- HTTP 408, 425, 429, 500, 502, 503, 504
- Network errors from `requests`

**Permanent** (dead-letter immediately):

- Integrity constraint violations: `unique_violation`,
  `foreign_key_violation`, `check_violation` (these mean the payload
  is wrong; retrying won't help)
- Schema errors: `undefined_table`, `undefined_column`,
  `datatype_mismatch`
- Privilege failures: `insufficient_privilege` (42501). A grant is
  missing; fix the grant, then requeue the dead-lettered items.

**Authentication failures are transient.** `invalid_password` (28P01) and
`invalid_authorization_specification` (28000) mean the credentials are wrong
or were rotated. They are retried with backoff, not dead-lettered, so a
password rotation does not dead-letter every pending submission. Correct the
secret and the queue drains on its own.
- HTTP 4xx (except 408, 425, 429)

**Unknown** — classified as transient and retried with capped backoff.
Unrecognised errors are never dead-lettered automatically, so a bug cannot
silently discard a submission. There is no retry limit that abandons an item.

## Worker lifecycle

The worker is a single daemon thread that runs inside each gunicorn
worker process when `SUPABASE_WORKER_ENABLED=1`. It:

1. Polls `claim_pending(batch_size=10)` every 5 seconds.
2. For each claimed item, calls the appropriate handler.
3. On success, calls `mark_delivered(item.id)`.
4. On a transient failure, calls `mark_failed(...)` with the next
   `next_attempt_at` computed from `compute_backoff()`.
5. On a permanent failure, calls `dead_letter(item.id, reason=...)`.
6. If an iteration itself fails (for example the local SQLite file is
   briefly locked), the loop backs off exponentially, capped at 300 seconds,
   and keeps running. It never exits on its own. Earlier versions stopped
   after 50 consecutive failures, which silently ended delivery.

The worker is safe to run in multiple processes against the same
outbox file (SQLite WAL + busy_timeout handle locking) and against
the same Supabase `private.delivery_queue` (FOR UPDATE SKIP LOCKED).

## Render deployment: durability limitations

### Honest durability contract

The application NEVER claims a contact submission was "saved" to a
volatile queue. The contact route reads `outbox.is_durable()` (which
checks the `OUTBOX_DURABLE` env var, default `0` = best-effort) and
returns one of these statuses:

- `queued_durable` — the operator has set `OUTBOX_DURABLE=1` AND
  mounted a persistent volume at `SUPABASE_QUEUE_PATH`. Submissions
  survive instance replacement.
- `queued_volatile` — the queue lives on the container's ephemeral
  filesystem. Submissions survive gunicorn worker restarts but are
  LOST on instance replacement. The user is told this in plain
  English in the success message.
- `rejected` — the queue is full and the contact submission could not
  be persisted (this should never happen because contacts always
  bypass the capacity check, but the code path exists defensively).

The default is `queued_volatile`. Setting `OUTBOX_DURABLE=1` without
actually mounting a persistent volume is **a lie** — the application
will tell users their submissions are durably queued when in fact they
are not. The operator MUST verify the persistent disk is mounted
before setting `OUTBOX_DURABLE=1`.

### What "ephemeral filesystem" means on Render

**On Render free tier and Render Starter+ without a persistent disk**,
the container's filesystem (including `/tmp`) is ephemeral:

- ✅ Survives gunicorn worker restarts within the same instance.
- ✅ Survives a single request-handler crash.
- ❌ Does NOT survive instance replacement (Render free tier recycles
  instances after 15 minutes of inactivity, on every deploy, and on
  platform maintenance).
- ❌ Does NOT survive a deploy that replaces the container.
- ❌ Two separate Render services do NOT share a filesystem. The web
  service and the Background Worker each get their own ephemeral
  filesystem. Writing to `/tmp/anvil-outbox.sqlite3` from the web
  service is invisible to the Background Worker, and vice versa.

When the web service is replaced, all pending outbox rows on its
ephemeral filesystem are lost. **Contact submissions in the lost
outbox are gone forever** — they were never delivered to Supabase and
cannot be recovered.

### Production upgrade path

Pick ONE of the following options, in order of preference. Each option
specifies the **exact** architecture required for the web service and
the background worker to share durable queue data.

#### Option A: Single-instance web service + persistent disk (simplest durable)

```
┌─────────────────────────────────────────────────────┐
│  Render web service (single gunicorn worker)        │
│                                                     │
│  ┌──────────────────────┐  ┌────────────────────┐  │
│  │  Flask app           │  │  delivery worker   │  │
│  │  (handles HTTP)      │  │  (in-process       │  │
│  │  enqueues to SQLite  │  │   thread, polls    │  │
│  │  on the disk         │  │   & delivers)      │  │
│  └──────────┬───────────┘  └─────────┬──────────┘  │
│             │                          │             │
│             └──────────┬───────────────┘             │
│                        │                             │
│                        ▼                             │
│  ┌─────────────────────────────────────────────────┐│
│  │  Persistent disk mounted at /var/lib/anvil      ││
│  │  /var/lib/anvil/outbox.sqlite3 (WAL mode)      ││
│  │  Survives instance replacement.                ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

Required configuration:
1. Render web service on a paid plan (Starter or higher) that supports
   persistent disks.
2. Mount a persistent disk at `/var/lib/anvil`.
3. Set the following env vars on the web service:
   - `SUPABASE_QUEUE_PATH=/var/lib/anvil/outbox.sqlite3`
   - `OUTBOX_DURABLE=1`
   - `SUPABASE_WORKER_ENABLED=1` (in-process worker)
   - `WEB_CONCURRENCY=1` (single gunicorn worker, see below)
4. Restart the web service.

Why `WEB_CONCURRENCY=1`: multiple gunicorn workers each fork a child
process, and each child would start its own worker thread competing on
the same SQLite file. SQLite WAL locking handles this safely, but it
wastes resources. For higher throughput, scale horizontally (more
containers, each with its own disk — see Option B) rather than
vertically (more gunicorn workers in one container).

#### Option B: Web service + separate Background Worker (recommended for production)

Render disks can be attached to only ONE service at a time. The web
service and the Background Worker therefore CANNOT share a SQLite
file via a persistent disk. Two valid sub-configurations:

##### B1. Both services share an external durable queue (Redis/SQS/Cloud Tasks)

```
┌─────────────────────────────┐     ┌─────────────────────────────┐
│  Render web service         │     │  Render Background Worker   │
│                             │     │                             │
│  ┌──────────────────────┐   │     │   ┌──────────────────────┐ │
│  │  Flask app           │   │     │   │  delivery.worker_cli  │ │
│  │  enqueues to Redis   │───┼─────┼──▶│  claims from Redis    │ │
│  └──────────────────────┘   │     │   └──────────┬───────────┘ │
└─────────────────────────────┘     │              │             │
                                    │              ▼             │
                                    │   ┌──────────────────────┐ │
                                    │   │  Redis (or SQS)      │ │
                                    │   │  Durable, network-   │ │
                                    │   │  accessible queue     │ │
                                    │   └──────────────────────┘ │
                                    └─────────────────────────────┘
                                              │
                                              ▼
                                    ┌──────────────────────┐
                                    │  Supabase Postgres  │
                                    └──────────────────────┘
```

Required configuration:
1. Provision a Redis instance (Render Redis, Upstash, AWS ElastiCache,
   or any Redis-compatible service).
2. Replace `delivery/outbox.py` with a Redis-backed implementation
   (the interface is small: `enqueue`, `claim_pending`, `mark_delivered`,
   `mark_failed`, `dead_letter`, `requeue_dead_letter`, `counts`,
   `list_recent`, `list_dead_lettered`).
3. Both web service and Background Worker connect to the SAME Redis URL.
4. `SUPABASE_WORKER_ENABLED=0` on the web service.
5. Background Worker runs `python -m delivery.worker_cli --batch 10
   --interval 5`.

This is the only architecture that lets the web service and the
Background Worker scale independently while sharing durable queue state.

##### B2. Web service has its own disk; Background Worker is NOT used

```
┌─────────────────────────────────────────────────────┐
│  Render web service (single gunicorn worker)        │
│                                                     │
│  ┌──────────────────────┐  ┌────────────────────┐  │
│  │  Flask app           │  │  delivery worker   │  │
│  │  enqueues to disk    │  │  (in-process)      │  │
│  └──────────┬───────────┘  └─────────┬──────────┘  │
│             └──────────┬───────────────┘             │
│                        ▼                             │
│  ┌─────────────────────────────────────────────────┐│
│  │  Persistent disk (one service only)             ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

This is functionally identical to Option A. If you don't need a separate
Background Worker, skip it — the in-process worker handles delivery.

#### Option C: Web service on free tier (best-effort, NOT durable)

```
┌─────────────────────────────────────────────────────┐
│  Render web service (free tier, no persistent disk) │
│                                                     │
│  ┌──────────────────────┐  ┌────────────────────┐  │
│  │  Flask app           │  │  delivery worker   │  │
│  │  enqueues to /tmp    │  │  (in-process)      │  │
│  └──────────┬───────────┘  └─────────┬──────────┘  │
│             └──────────┬───────────────┘             │
│                        ▼                             │
│  ┌─────────────────────────────────────────────────┐│
│  │  /tmp/anvil-outbox.sqlite3 (EPHEMERAL)         ││
│  │  Lost on instance replacement.                 ││
│  └─────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────┘
```

In this mode:
- `OUTBOX_DURABLE=0` (the default).
- The contact form honestly tells users the submission is
  "queued locally (best-effort)".
- The admin dashboard shows a red "best-effort" warning.
- The operator should drain the queue (`python -m delivery.worker_cli
  --once` repeatedly) before any deploy.

**This is the only mode that works without paying for a persistent
disk. It is honest about its limitations.**

### Verifying the disk is actually persistent

Before setting `OUTBOX_DURABLE=1`, the operator MUST verify:

1. The Render disk is mounted at `SUPABASE_QUEUE_PATH`'s parent
   directory.
2. Writing a file, then restarting the service, leaves the file in
   place.

```bash
# On the Render shell (Dashboard → Shell):
echo "test" > /var/lib/anvil/persistence-test
# Restart the service via the Render dashboard, then:
cat /var/lib/anvil/persistence-test  # must print "test"
```

If the file is gone after restart, the disk is NOT actually persistent
and `OUTBOX_DURABLE` must remain `0`.

## Queue monitoring

The admin dashboard at `/admin/queue` shows:

- Pending / in-flight / retrying / delivered / dead-lettered counts.
- Contact-pending vs analytics-pending (so the operator can spot
  when contact submissions are stuck).
- The oldest pending item's age in seconds.
- Supabase connectivity (configured + reachable).
- The 100 most recent items and the 50 most recent dead-lettered
  items.
- A "Requeue" button for each dead-lettered item.

A separate `/api/admin/healthz` endpoint (admin-auth required) returns
the same data as JSON for uptime-monitoring integrations.

## Manual operations

```bash
# Process one batch and exit (useful for cron)
python -m delivery.worker_cli --once --batch 50

# Run forever (background worker)
python -m delivery.worker_cli --batch 10 --interval 5

# Inspect the outbox directly
sqlite3 $SUPABASE_QUEUE_PATH \
  "SELECT event_type, COUNT(*) FROM outbox \
   WHERE delivered_at IS NULL AND dead_lettered_at IS NULL \
   GROUP BY event_type;"

# Requeue all dead-lettered items (operator recovery)
sqlite3 $SUPABASE_QUEUE_PATH \
  "UPDATE outbox SET dead_lettered_at = NULL, dead_letter_reason = NULL, \
   next_attempt_at = strftime('%s','now') \
   WHERE dead_lettered_at IS NOT NULL;"
```

## Failure scenarios and behaviour

| Scenario                                       | Browser sees                       | Queue state                                   |
|------------------------------------------------|------------------------------------|-----------------------------------------------|
| Supabase online, payload valid                 | 202 + reference code               | Item delivered immediately                    |
| Supabase paused (free tier inactivity)         | 202 + reference code               | Item pending; worker retries every few sec    |
| Supabase TLS error / connection refused        | 202 + reference code               | Item pending; classified transient, retried  |
| Invalid payload (bad event_name, missing uuid) | 400 with error message            | Nothing queued                                |
| Rate-limited                                    | 429 + retry_after_seconds         | Nothing queued                                |
| Outbox full + analytics event                  | 202 (event silently dropped)      | An older analytics event evicted              |
| Outbox full + contact submission              | 503 + "please retry"             | Nothing queued; user told to retry           |
| Permanent DB error (RLS denial)                | 202 (initially), then dead-letter | Item dead-lettered; admin can requeue        |
| Worker process killed mid-delivery             | 202 (already returned)            | Item still in `pending` (claim was rolled back); next worker picks it up |
| Disk full / SQLite locked                      | 503 + "please retry"             | Item NOT queued; user told to retry          |


## Update: exclusive claims, leases, and fork safety

These changes were verified with the test suite and reproduction scripts
(see `tests/test_hardening_regressions.py`).

- **Exclusive claims.** `claim_pending` selects and updates inside one
  `BEGIN IMMEDIATE` transaction. Earlier code used separate autocommit
  statements, and four processes sharing the SQLite file produced about 25%
  duplicate claims. Testing now shows zero duplicates.
- **Leases.** Each claimed row gets `lease_owner` and `lease_expires_at`
  (default 300 s, `SUPABASE_QUEUE_LEASE_SECONDS`, clamped to 30–3600 s).
  Delivered, failed, and dead-lettered transitions release the lease. A crashed
  worker's lease expires and the item becomes claimable again. Nothing is left
  stuck in flight. Set the lease longer than the worst-case time to process one
  batch. A slow worker whose lease expires may cause a duplicate attempt, which
  the idempotency keys absorb.
- **Existing queues** are upgraded in place (columns added, rows preserved).
- **Classification.** psycopg 3 exposes SQLSTATE as `sqlstate`. Earlier code read
  `pgcode`, which is psycopg 2's name, so no database error was ever classified
  by SQLSTATE. Permanent schema and constraint errors are now dead-lettered
  as designed.
- **Fork safety.** Under gunicorn, each worker process starts its own delivery
  thread. The child resets inherited locks and discards the inherited database
  connection. Do not use `--preload`.
- **TLS.** Connections use `verify-full`. See the TLS section of
  `docs/security-review.md`.
