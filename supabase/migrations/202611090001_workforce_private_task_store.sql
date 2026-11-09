-- Autonomous AI Workforce V3
-- Apply once in the Supabase SQL Editor or through the Supabase migration workflow.
-- Application connection: DATABASE_URL (backend only), using Supabase Shared Pooler.
BEGIN;

CREATE SCHEMA IF NOT EXISTS workforce_private;

CREATE TABLE IF NOT EXISTS workforce_private.workforce_tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at VARCHAR(64) NOT NULL
);

ALTER TABLE workforce_private.workforce_tasks ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON SCHEMA workforce_private FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE workforce_private.workforce_tasks FROM PUBLIC, anon, authenticated;

COMMIT;
