--liquibase formatted sql

--changeset cicd:002-app-grants runAlways:true
-- App access to a synced table = EXPLICIT native Postgres grants, reapplied by CI after
-- each synced-table create/replace. NOT `ALTER DEFAULT PRIVILEGES` (blocked: the writer
-- role databricks_writer_<dbid> owns synced tables, and users cannot set its defaults or
-- join it) and NOT Unity Catalog grants (no documented UC->PG app-role path -- UC governs
-- only the creating identity).
-- MUST be runAlways:true (not runOnChange): GRANT is idempotent (no-op if already held), and
-- a sync-mode change forces a destroy+recreate that drops grants. runOnChange would see an
-- unchanged checksum and SKIP, leaving the app without SELECT after a replace. runAlways
-- reapplies on every deploy = the "never lose access" guarantee actually holds.
GRANT USAGE  ON SCHEMA ${app_schema}                  TO ${app_role};
GRANT SELECT ON TABLE  ${app_schema}.${synced_table}  TO ${app_role};
--rollback REVOKE SELECT ON TABLE ${app_schema}.${synced_table} FROM ${app_role};
--rollback REVOKE USAGE ON SCHEMA ${app_schema} FROM ${app_role};
