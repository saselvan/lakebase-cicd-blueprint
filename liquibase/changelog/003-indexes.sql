--liquibase formatted sql

--changeset cicd:003-indexes runAlways:true
-- Applied AFTER the initial load (CI waits for the first sync to reach ONLINE, then runs
-- `liquibase update`). runAlways:true + `CREATE INDEX IF NOT EXISTS` => idempotent no-op on a
-- normal deploy, but REBUILDS the indexes after a table replace (a sync-mode change forces
-- destroy+recreate, dropping indexes to pkey-only). runOnChange would skip the rebuilt table
-- on the same instance, so runAlways is required here too.
--
-- Index columns are parametrized (${index_col_1}/${index_col_2}) and supplied per table by
-- scripts/deploy.sh from config/tables.json. When a table declares only one index column,
-- deploy.sh sets both params to the same column, so the second CREATE INDEX collapses to a
-- same-name no-op. This is the ONE inherently table-specific spot: a table needing more/fewer
-- indexes, a composite/partial index, or a different index type customizes THIS changeset.
--
-- For a ZERO-DOWNTIME rebuild on a large live table, use CREATE INDEX CONCURRENTLY in its
-- own changeset with `runInTransaction:false` (concurrent index build cannot run inside a
-- transaction). Kept as plain CREATE INDEX here so the initial build runs in one tx.
CREATE INDEX IF NOT EXISTS idx_${synced_table}_${index_col_1} ON ${app_schema}.${synced_table} (${index_col_1});
CREATE INDEX IF NOT EXISTS idx_${synced_table}_${index_col_2} ON ${app_schema}.${synced_table} (${index_col_2});
--rollback DROP INDEX IF EXISTS ${app_schema}.idx_${synced_table}_${index_col_1};
--rollback DROP INDEX IF EXISTS ${app_schema}.idx_${synced_table}_${index_col_2};
