-- Repair: add columns that older databases are missing from private.audit_log.
-- Safe to run more than once. Run in the Supabase SQL Editor.
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS table_schema TEXT;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS table_name   TEXT;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS operation    TEXT;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS row_pk       TEXT;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS changed_by   TEXT;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS before_data  JSONB;
ALTER TABLE private.audit_log ADD COLUMN IF NOT EXISTS after_data   JSONB;
