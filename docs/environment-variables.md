# Environment variables

Values are never stored in the repository. `.env.example` lists names and
placeholders. "Server-only" means the value must never reach a browser,
template, log line, or public response.

## Required in production

| Variable | Scope | Notes |
|---|---|---|
| `SECRET_KEY` | server-only | At least 32 chars, stable across restarts. Signs admin session cookies. |
| `SITE_URL` | public | Canonical `https://` origin. Enables `Secure` cookies and HSTS. |
| `DATABASE_URL` | server-only | Postgres DSN. Must use `sslmode=verify-full`. |
| `SUPABASE_SSL_ROOT_CERT` | server-only | Path to Supabase's root CA file (a Render Secret File). |
| `SUPABASE_URL` | public | Project URL; used to verify admin tokens. |
| `SUPABASE_ANON_KEY` | public | Anon key. Public by design, but not needed in templates. |
| `SUPABASE_SERVICE_ROLE_KEY` | server-only | Bypasses RLS. Never expose. |
| `IP_HMAC_SECRET` | server-only | At least 16 chars. Keys IP pseudonyms. Rotating it breaks continuity of IP identifiers. |
| `TRUSTED_PROXY_COUNT` | server-only | `1` on Render; `0` if reached directly. |

## Database and TLS

| Variable | Default | Notes |
|---|---|---|
| `SUPABASE_DB_HOST` | project host | Override for a pooler. Confirm the exact host in the Supabase dashboard. |
| `SUPABASE_DB_PORT`, `SUPABASE_DB_USER`, `SUPABASE_DB_NAME` | `5432`, `postgres`, `postgres` | Used when `DATABASE_URL` is unset. |
| `SUPABASE_DB_PASSWORD` | none | Used when `DATABASE_URL` is unset. The password is URL-encoded. |
| `SUPABASE_CONNECT_TIMEOUT` | `5` | Seconds, 1–60. |
| `SUPABASE_STATEMENT_TIMEOUT` | `10` | Seconds, 1–300. |

Refused configurations: `sslmode` set to `require`, `prefer`, `allow`, or
`disable`. The connection is treated as unconfigured and an error is logged.

## Queue and worker

| Variable | Default | Notes |
|---|---|---|
| `SUPABASE_QUEUE_PATH` | `/tmp/anvil-outbox.sqlite3` | Ephemeral on Render unless a disk is mounted. |
| `OUTBOX_DURABLE` | `0` | Set `1` only when the queue path is on persistent storage. |
| `SUPABASE_WORKER_ENABLED` | `0` (local), `1` (render.yaml) | In-process worker. Only one instance. |
| `SUPABASE_WORKER_POLL_SECONDS` | `5` | Minimum 1. |
| `SUPABASE_QUEUE_LEASE_SECONDS` | `300` | Clamped to 30–3600. Must exceed one batch's worst-case time. |
| `SUPABASE_RETRY_BASE_DELAY_SECONDS` | `2` | Clamped to 0.1–60. |
| `SUPABASE_RETRY_MAX_DELAY_SECONDS` | `900` | Upper bound on one backoff. |
| `SUPABASE_MAX_QUEUE_ITEMS` | `10000` | Analytics are evicted first. Contact submissions are never evicted. |
| `SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS` | `0` | `0` disables pruning. Contact submissions are never pruned. |
| `SUPABASE_QUEUE_SATURATION_ALERT_PCT` | `80` | `0` disables the warning. |
| `SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN` | `15` | Minimum 1 minute. |

## Optional

| Variable | Notes |
|---|---|
| `FLASK_ENV`, `FLASK_DEBUG` | Set `production` and `0`. Debug must be off in production. |
| `PORT` | Set by Render. |
| `WEB_CONCURRENCY` | `1`. More workers multiply in-process worker threads. |
| `MAX_CONTENT_LENGTH_BYTES` | Default 5 MB. |
| `ANALYTICS_PROVIDER`, `ANALYTICS_ID` | Third-party analytics script. Adds an external origin. |
| `ADSENSE_CLIENT_ID` | Enables advertising. Adds external origins. |

## Startup validation

Startup does not stop on missing configuration. Behaviour:

- A missing or short `SECRET_KEY` logs a warning and uses a random per-process
  key. Admin sessions then end on every restart, and multiple instances cannot
  share sessions. This is a warning, not a refusal, so check the logs after deploy.
- Missing Supabase configuration: pages still serve, and queued submissions wait
  in the outbox. The admin dashboard reports the state.
