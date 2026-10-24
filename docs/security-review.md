# Security review

Scope: the Flask application, the admin dashboard, the contact and analytics
APIs, the durable outbox and worker, and the Supabase schema in `migrations/`.
Every finding below was reproduced or traced to code before it was fixed. The
"Evidence" column names the test that now guards it.

## Fixed in this review

| # | Finding | Severity | Fix | Evidence |
|---|---------|----------|-----|----------|
| 1 | `sslmode=require` encrypts without verifying the server certificate. Reproduced: an untrusted-CA certificate was accepted. | High | Default `verify-full`. Unverified modes are refused. `SUPABASE_SSL_ROOT_CERT` fails closed if the file is missing. | `test_hardening_regressions.py::TestTlsVerification` |
| 2 | Any signed-in Supabase user could execute `public.upsert_analytics_daily` through PostgREST. | High | Migration 005 restricts EXECUTE to `service_role`. | `test_rls_access.py::test_authenticated_cannot_execute_analytics_upsert` |
| 3 | No RLS on application tables in the repository's migrations. | High | Migration 005 enables RLS and defines admin-only SELECT (and narrow UPDATE on `support_queries`) through `private.is_admin()`. | `test_rls_access.py` (12 behavioral tests on real PostgreSQL) |
| 4 | RLS policies would have errored for `authenticated`, because the role lacked `USAGE` on schema `private`. | High (would have broken admin) | `GRANT USAGE ON SCHEMA private TO authenticated, service_role`. | `test_rls_access.py::test_non_admin_sees_no_support_rows` |
| 5 | X-Forwarded-For spoofing. The client-IP extraction read one hop too far, so an attacker could pick the IP that rate limits were keyed on. | High | Trusted-proxy convention corrected. `TRUSTED_PROXY_COUNT` is read from the environment. | `test_analytics_privacy.py::TestExtractClientIp`, `test_hardening_regressions.py::TestLimiterIdentity` |
| 6 | Contact rate limit fell back to a client-chosen cookie when `IP_HMAC_SECRET` was unset. | Medium | Limiter key is an HMAC of the trusted client IP, using the configured secret or a per-process random key held in memory only. | `TestLimiterIdentity::test_cookie_does_not_change_contact_limiter_key` |
| 7 | Analytics limiter shared one bucket (`anon:unknown`) for every visitor when the secret was unset. | Medium | Same helper as #6. | `TestLimiterIdentity::test_no_shared_bucket_without_hmac_secret` |
| 8 | A single global key on admin login let any anonymous caller lock out all admins. | Medium | Per-client key. | `TestAdminLoginThrottle` |
| 9 | Every rate-limited request enqueued an unbounded `admin_audit` row (queue-flood DoS). | Medium | Throttled in-process logging, no queue writes. | `test_rate_limit_events_do_not_write_to_outbox` |
| 10 | Admin write endpoints had no CSRF token. SameSite=Lax and the JSON content-type check gave partial protection, but not an explicit control. | Medium | Session-bound token, required as `X-CSRF-Token` or a form field, compared in constant time. Enforced on login, logout, support update, and queue retry. | `tests/test_csrf.py` (14 tests) |
| 11 | `SESSION_COOKIE_SECURE` was never set, although a comment said it was. | Medium | Set when `SITE_URL` is HTTPS. | `test_session_cookie_is_secure_for_https_site` |
| 12 | Session not rotated on admin login. | Low | `session.clear()` before the new session starts. | Code review; covered by admin login tests |
| 13 | Dependency vulnerabilities: `requests` 2.32.3 (PYSEC-2026-1872, PYSEC-2026-2275). | Medium | Pinned to 2.33.0. `pip-audit` reports no known vulnerabilities. | `pip-audit -r requirements-dev.txt` |
| 14 | Psql meta-commands in migrations would break a paste into the Supabase SQL Editor, and the documentation wrongly said the editor ignores them. | Low | Removed. The runner passes `-v ON_ERROR_STOP=1`. | `test_migrations.py::test_migrations_contain_no_psql_meta_commands` |

Reliability defects with security impact (duplicate claims, worker silently
stopping, misclassified errors) are covered in `docs/supabase-retry-and-queue.md`.

## Authentication and authorization

- Admin identity: a Supabase access token is verified server-side against
  `/auth/v1/user`. The admin decision is made by `private.is_admin()` on every
  request. Client-supplied IDs and flags are never trusted.
- Admin sessions: signed Flask cookie, 4-hour TTL, `HttpOnly`, `SameSite=Lax`,
  `Secure` when `SITE_URL` is HTTPS.
- Login throttle: 20 attempts per minute per client, keyed on the trusted client
  IP.

## Residual risks (not fixed here, must be accepted or addressed)

1. **Content Security Policy allows `'unsafe-inline'` scripts and several
   third-party ad and analytics hosts.** The no-build templates depend on inline
   scripts, and the policy also permits advertising origins. Reducing this needs
   nonces or hashes for every inline script and a decision on whether ads remain.
2. **The database role in `DATABASE_URL` decides whether RLS applies.** The Flask
   app and the worker connect as a role that bypasses RLS (the `postgres` role).
   RLS protects only PostgREST access with the `anon` and `authenticated` keys.
   Confirm the role in production before relying on RLS for the app.
3. **Migration 005 has not been applied to any live database.** Until it is, the
   live RLS and grant state is unknown. Verify it from the catalog first.
4. **Database operators can alter the audit log.** `private.audit_log` is
   database-local. It is not tamper-evident and is not a backup.
5. **Rate limits are per process.** With several instances the effective limit
   multiplies. Put a CDN or WAF in front for stricter limits.
6. **The admin login uses a pasted access token.** This is acceptable for a
   single operator. Replace it with an OAuth redirect before adding administrators.
7. **Trusted-proxy count must match the platform.** A wrong
   `TRUSTED_PROXY_COUNT` either ignores real clients or trusts forged headers.
   Verify it on the deployed platform.

## TLS configuration

1. Download Supabase's root CA from Project Settings → Database → SSL.
2. Store it as a secret file, for example `/etc/secrets/supabase-ca.crt`.
3. Set `SUPABASE_SSL_ROOT_CERT=/etc/secrets/supabase-ca.crt`.
4. Use `sslmode=verify-full` in `DATABASE_URL`, or leave the mode unset, which
   defaults to `verify-full`.

If the connection is refused, the application logs the reason and the queue
keeps its items. Do not work around it by switching to `require`.
