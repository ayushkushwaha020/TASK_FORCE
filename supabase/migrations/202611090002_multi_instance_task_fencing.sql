-- V3 multi-instance coordination: canonical task versions and worker fencing.
-- Safe to run against an existing workforce_private.workforce_tasks table.
ALTER TABLE workforce_private.workforce_tasks
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 1;

ALTER TABLE workforce_private.workforce_tasks
    ADD COLUMN IF NOT EXISTS execution_fence BIGINT NOT NULL DEFAULT 0;
