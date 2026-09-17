--liquibase formatted sql

--changeset cicd:003-indexes runAlways:true
-- Applied AFTER the initial load (CI waits for the first sync to reach ONLINE, then runs
-- `liquibase update`). runAlways:true + `CREATE INDEX IF NOT EXISTS` => idempotent no-op on a
-- normal deploy, but REBUILDS the indexes after a table replace (a sync-mode change forces
-- destroy+recreate, dropping indexes to pkey-only). runOnChange would skip the rebuilt table
-- on the same instance, so runAlways is required here too.
--
-- For a ZERO-DOWNTIME rebuild on a large live table, use CREATE INDEX CONCURRENTLY in its
-- own changeset with `runInTransaction:false` (concurrent index build cannot run inside a
-- transaction). Kept as plain CREATE INDEX here so the initial build runs in one tx.
CREATE INDEX IF NOT EXISTS idx_${synced_table}_member_id ON ${app_schema}.${synced_table} (member_id);
CREATE INDEX IF NOT EXISTS idx_${synced_table}_plan_code ON ${app_schema}.${synced_table} (plan_code);
--rollback DROP INDEX IF EXISTS ${app_schema}.idx_${synced_table}_member_id;
--rollback DROP INDEX IF EXISTS ${app_schema}.idx_${synced_table}_plan_code;
