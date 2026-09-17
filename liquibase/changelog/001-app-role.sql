--liquibase formatted sql

--changeset cicd:001-app-role runOnChange:false splitStatements:false
-- App-specific read-only role. Idempotent so a fresh instance rebuild is safe.
-- splitStatements:false: the DO block has internal semicolons; send it as ONE statement.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${app_role}') THEN
    CREATE ROLE ${app_role} NOLOGIN;
  END IF;
END $$;
--rollback DROP ROLE IF EXISTS ${app_role};
