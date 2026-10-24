# Database migrations

## Order

Apply in this order. Migrations 001–006 are for the live project.

| File | Purpose | Idempotent | Notes |
|---|---|---|---|
| `000_base_schema.sql` | **Test-only.** Reproduces the pre-existing schema. | n/a | **Never apply to production.** |
| `001_schema_verification.sql` | Verifies expected tables and columns. | yes | Raises notices, not schema changes. |
| `002_missing_constraints_and_indexes.sql` | Analytics unique index, indexes. | yes | Aborts if duplicate analytics rows exist. Resolve them manually first. |
| `003_retry_queue_idempotency.sql` | `private.delivery_queue` and claim function. | yes | Enables RLS on the queue table. |
| `004_admin_analytics_support.sql` | Support columns, reference code, upsert function. | yes | Its upsert stored `''` as a separate key. Corrected by 006. |
| `005_rls_and_least_privilege.sql` | RLS, admin-only policies, grants. | yes | Changes live access. Requires catalog review first. |
| `006_analytics_upsert_normalization.sql` | Corrected upsert function. | yes | Forward-only. Changes no stored data. |

## Running

With `psql` (preferred for repeatable runs):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_schema_verification.sql
```

In the Supabase SQL Editor: paste the whole file and run it. The files contain
no psql meta-commands. Read the output: success means no `ERROR` lines, not a
blank result.

## Before running on live

1. Take and verify a backup (see `docs/backup-and-restore.md`). Status today:
   **NOT VERIFIED**.
2. Capture the current grants and policies:
   `\dp public.*` and `SELECT * FROM pg_policies;` in psql. Save the output.
3. Run the migration on a staging copy of the project first.
4. Compare the staging result with the expected state in the migration header.

## Failure midway

Every migration runs in one transaction (`BEGIN`/`COMMIT`). A failure rolls back
the whole file. Re-run after fixing the cause. Migration 002 deliberately stops if
duplicate analytics rows exist. Resolve them by hand. Never delete rows
automatically.

## Upgrade paths tested

Tested on PostgreSQL 16 (`tests/test_migrations_e2e.py`,
`tests/test_analytics_upsert_pg.py`):

- Fresh database, 000 then 001–006.
- Database at 004 (live state), upgraded with 005 and 006.
- Re-running 005 and 006 (idempotent).

Not tested: a live Supabase project. Supabase's own `auth` schema and role
definitions are stubbed in the test database.
