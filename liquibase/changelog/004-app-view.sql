--liquibase formatted sql

--changeset cicd:004-app-view runAlways:true
-- NO-SUPERUSER consumer pattern.
--
-- The service principal that creates the synced table automatically owns it and gets SELECT.
-- That same identity runs this changeset, so it can CREATE a view over the synced table and,
-- because it OWNS the view, GRANT other consumers SELECT on the VIEW -- all without ever
-- needing databricks_superuser or membership in the managed writer role (which is un-grantable).
--
-- Consumers read the view, not the base synced table. This keeps the access grant on an object
-- the deploy identity owns, so grants never depend on privileges no user can hold.
--
-- runAlways:true so the view and its grants are recreated after a synced-table replace (a
-- sync-mode change forces a destroy+recreate that drops the base table and everything on it).
CREATE OR REPLACE VIEW ${app_schema}.${synced_table}_v AS
  SELECT * FROM ${app_schema}.${synced_table};

GRANT USAGE  ON SCHEMA ${app_schema}                       TO ${app_role};
GRANT SELECT ON ${app_schema}.${synced_table}_v            TO ${app_role};
--rollback DROP VIEW IF EXISTS ${app_schema}.${synced_table}_v;
