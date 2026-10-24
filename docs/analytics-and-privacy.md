# Analytics and privacy

This document explains what data the Anvil DevTools analytics system
collects, what it deliberately does *not* collect, who can see it, and
how long it is kept. It is intended to be readable by both operators
and end users.

## What we collect

Per page view, tool run, and tool result, the analytics tracker
(`static/js/analytics.js`) sends the following fields to
`POST /api/analytics/event`:

| Field             | Source          | Example                          | Notes                                                                  |
|-------------------|-----------------|----------------------------------|------------------------------------------------------------------------|
| `event_name`      | Browser         | `page_view`, `tool_run`          | Must be in the allow-list; rejected otherwise.                        |
| `session_id`      | Browser (UUID) | `00000000-...-000000000001`      | First-party, random, regenerated after 30 min inactivity.              |
| `tool_slug`       | Browser         | `json-formatter`                 | Optional, only for tool events.                                        |
| `page_path`       | Browser         | `/workbench`                     | Path only — never the query string.                                    |
| `properties`      | Browser         | `{"format": "json", "duration_ms": 12}` | Allow-listed keys only, see below.                              |
| `occurred_at`     | Browser         | `1716489600`                     | Unix epoch seconds.                                                     |
| `ip_hmac`         | Server          | `a3f1...` (64 hex chars)         | HMAC-SHA256 of the client IP, server-side, never the raw IP.           |

The session upsert (`POST /api/analytics/session`) sends:

| Field                 | Source  | Notes                                                                |
|-----------------------|---------|----------------------------------------------------------------------|
| `visitor_id`          | Browser | First-party UUID, persisted in `localStorage`.                        |
| `session_id`          | Browser | First-party UUID, rotated every 30 min inactivity.                   |
| `started_at`          | Browser | When the session started.                                            |
| `last_seen_at`        | Browser | Last user interaction.                                               |
| `engagement_seconds`  | Browser | Cumulative time the page was visible, paused when hidden.            |
| `landing_page`        | Browser | First page of the session (path only, ≤ 2048 chars).                |
| `referrer_domain`     | Browser | Hostname of the referrer (NOT the full URL — query strings dropped). |
| `device_type`         | Browser | One of `desktop`, `mobile`, `tablet`, `unknown`.                    |
| `country_code`        | Server  | Optional, ISO 3166-1 alpha-2.                                        |
| `ip_hmac`             | Server  | HMAC-SHA256 of the client IP.                                        |

## What we DO NOT collect

The following are explicitly forbidden in the analytics payload and
the server-side validator (`analytics/tracking.py:sanitize_properties`)
strips them:

- **Tool input.** The JSON, XML, YAML, CSV, SQL, or Markdown you paste
  into a tool is never sent to the analytics endpoint.
- **Tool output.** Same — only the *size* (`input_size_bytes`,
  `result_size_bytes`) and the outcome (`ok`/`error`) are recorded.
- **JWTs, API keys, cookies, auth headers.** The browser tracker never
  reads them.
- **Raw IP addresses.** Only the HMAC of the IP is stored, and only if
  `IP_HMAC_SECRET` is configured (≥ 16 chars). Without the secret,
  IP-derived fields are skipped entirely.
- **Full referrer URLs.** Only the registered domain is stored; UTM
  parameters and other query strings are dropped.
- **Browser fingerprints.** We do not collect canvas, font, WebGL,
  audio, or battery fingerprints. The visitor ID is a random UUID
  generated in the browser, not a hash of attributes.
- **Cross-site identifiers.** The session ID is first-party; it is
  not shared with third parties and is not used for ad targeting.

## Allow-listed property keys

Only the following keys may appear in the `properties` JSONB column:

```
format, operation, action, duration_ms, input_size_bytes,
result_size_bytes, outcome, error_code, vote_count
```

Any other key is silently dropped by the validator. Strings are
truncated to 80 chars; integers are clamped to `±2,000,000,000`; lists,
dicts, and `null` are dropped. The total payload size is capped at
4000 bytes (the database CHECK is 4096 bytes, we leave headroom).

## IP HMAC and pseudonymity

The `ip_hmac` column stores `HMAC-SHA256(IP_HMAC_SECRET, ip)`, a
64-character hex string. It is **pseudonymous**, not anonymous:

- An attacker who exfiltrates the database *and* the `IP_HMAC_SECRET`
  can compute the HMAC for every IP they suspect and link it back to a
  row. The secret must therefore be stored separately from the
  database credentials and never committed to git.
- An attacker who exfiltrates only the database cannot reverse the
  HMAC to recover the IP (HMAC is one-way). They can however mount a
  dictionary attack against known IPs — the secret's length (≥ 16 chars)
  makes rainbow tables infeasible.
- Under GDPR and similar frameworks, pseudonymous data is still
  personal data when the controller holds the re-identification key.

**If `IP_HMAC_SECRET` is missing or shorter than 16 characters**, the
server sets `ip_hmac = NULL` for every event. The analytics still
works for traffic counting, but abuse-rate-limiting and country
attribution degrade.

## Rate limiting

Two in-memory rate limiters (`services/rate_limit.py`) protect the
public endpoints:

| Endpoint                  | Limit                            | Window |
|---------------------------|----------------------------------|--------|
| `POST /api/contact`       | 5 submissions per HMAC-of-IP     | 10 min |
| `POST /api/analytics/event` | 120 events per HMAC-of-IP      | 60 sec |
| `POST /api/analytics/session` | 120 events per HMAC-of-IP     | 60 sec |
| `POST /admin/login`       | 20 attempts per client IP        | 60 sec |

Limiter keys are an HMAC of the trusted-proxy client IP. When
`IP_HMAC_SECRET` is missing, a random per-process key is used instead,
so limiting still works. That key lives only in memory and is never
persisted. Clients cannot choose their bucket: cookies and
`X-Forwarded-For` values sent by the client are never used as keys.
Set `TRUSTED_PROXY_COUNT` to the number of proxies that append to
`X-Forwarded-For` (1 on Render).
In a multi-instance deployment the limiter is per-process and
approximate; for stricter limits, front the deployment with a CDN or
WAF.

## Retention

The application does NOT automatically delete analytics data from
PostgreSQL. The operator must decide on a retention policy and execute
it manually via SQL after documenting it. Suggested starting point:

```sql
-- Keep analytics_events for 18 months; older rows are aggregated into
-- analytics_daily (which is kept until the operator decides otherwise).
-- Adjust the interval to match your compliance window.
DELETE FROM public.analytics_events
 WHERE occurred_at < now() - interval '18 months';

-- Keep visitor_sessions for 90 days; older sessions are aggregated
-- into analytics_daily.
DELETE FROM public.visitor_sessions
 WHERE last_seen_at < now() - interval '90 days';

-- Keep security_events for 12 months.
DELETE FROM public.security_events
 WHERE occurred_at < now() - interval '12 months';

-- Audit log: do NOT delete as part of routine maintenance. Keep for
-- your compliance window (typically 1-7 years depending on jurisdiction).
```

**Do not run these statements without an explicit, documented
retention policy.** The spec says long-term retention is preferred.

### Outbox-side retention (separate from PostgreSQL retention)

The local SQLite outbox has its OWN retention policy for pending items,
independent of the PostgreSQL policy above:

| Item type            | Auto-pruned?                                     | Configurable?                          |
|----------------------|--------------------------------------------------|----------------------------------------|
| `contact_submission` | **NEVER**                                        | No — there is no env var to disable this guarantee |
| `analytics_event`    | Only if `SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS > 0` (default 0 = disabled) | Yes — set to a positive integer to enable, 0 to disable (the default) |
| `admin_audit`        | **NEVER**                                        | No — there is no env var to disable this guarantee |

**Default policy honors long-term retention.** Pruning is OFF by
default. The operator may opt in by setting
`SUPABASE_QUEUE_ANALYTICS_MAX_AGE_DAYS` to a positive integer; pending
analytics events older than that many days are then pruned on each new
analytics-event enqueue. Contact submissions are NEVER pruned by this
or any other mechanism.

When the outbox reaches `SUPABASE_MAX_QUEUE_ITEMS` (default 10,000), the
OLDEST pending analytics event is evicted to make room for a new one.
**Contact submissions are NEVER evicted to make room** — if the queue
is full of contacts, a new analytics event is rejected with
`QueueFullError` (and the analytics route silently drops it, since
analytics is best-effort). The user is never told about analytics
rejections.

### Saturation alerting

When pending items reach `SUPABASE_QUEUE_SATURATION_ALERT_PCT`
(default 80%) of `SUPABASE_MAX_QUEUE_ITEMS`:

- The worker logs a `WARNING` every
  `SUPABASE_QUEUE_SATURATION_ALERT_COOLDOWN_MIN` minutes (default 15)
  with the current pending/contact/analytics counts.
- The `/admin/queue` dashboard surfaces a saturation alert in the
  `counts()` payload (`saturation_alert: true`).
- The `saturation_pct` field is exposed in `counts()` so external
  monitoring (uptime checks, Prometheus scrape) can pick it up.

Set `SUPABASE_QUEUE_SATURATION_ALERT_PCT=0` to disable the alert.

This eviction policy is honest: the only data the outbox ever deletes
on its own is the analytics events, and only under explicit
configurable conditions. Contact submissions are preserved
indefinitely in the outbox until either delivered or dead-lettered.


## Security events

The `public.security_events` table records four event types:

- `rate_limited` — A client exceeded the rate limit.
- `invalid_payload` — A client sent a payload that failed validation.
- `auth_failure` — A Supabase token verification failed.
- `suspected_abuse` — Reserved for future heuristics.

Each event stores the IP HMAC (not the raw IP), the route name (≤ 120
chars), and a `details` JSONB object capped at 2048 bytes. The details
object MUST NOT contain user input or secrets — only metadata like
`{"reason": "rate_limited", "retry_after_seconds": 600}`.

## Admin access

All analytics tables are readable only by users in
`public.admin_profiles` (verified via `private.is_admin()`). The
admin dashboard at `/admin/*` enforces this on every request —
session cookies alone are never sufficient. See
`docs/supabase-retry-and-queue.md` for the auth flow.

## Browser controls

End users can:

- **Block the tracker** with a content blocker. The application still
  works without analytics — the failure is silent by design.
- **Clear their visitor ID** by clearing `localStorage` for the site.
  This does NOT delete historical analytics rows (those are server-side
  and keyed by the old UUID); it only prevents future events from
  being attributed to the same visitor.
- **Use private/incognito mode** — `localStorage` is isolated per
  session, so a fresh visitor ID is generated each time.

## Disclosure

This document is a technical reference. The end-user privacy policy at
`/privacy-policy` is the authoritative public statement. Update both
when the data collection changes — the policy and the code must agree.
