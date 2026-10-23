# ADR 0001 — View-based, no-superuser access to synced tables

**Status:** Accepted

## Context
Synced tables are owned by an internal managed role (`databricks_writer_<dbid>`). You cannot join
that role or change its default privileges — not even as the project owner (a member of
`databricks_superuser`; Lakebase does not grant customers a Postgres superuser role) — so `ALTER DEFAULT PRIVILEGES`
can't grant app access to future synced objects. A view needs only `SELECT` on the synced table, which its creator is granted. Granting the table itself to the app would also need the grant option.

## Decision
Grant app access through a **consumer view owned by the deploy identity**, not the base table.
The identity that creates a synced table is automatically granted `SELECT`; that same identity runs
Liquibase, so it `CREATE OR REPLACE VIEW`s over the synced table and grants consumers `SELECT` on the
**view**. Creator and view owner are kept the same identity.

## Consequences
- No `databricks_superuser` in the deploy path.
- Consumers read the view, never the base table — a clean indirection for future row filtering (see
  RLS note in DESIGN-NOTES).
- Requires discipline: the synced-table creator and the view owner must be the same principal.
- `CREATE ROLE` needs the `CREATEROLE` attribute. An admin creates each `NOLOGIN` app role once
  before the first deploy; changeset `001-app-role` (`role_guard_sql`) checks `pg_roles` and raises
  `app role <name> does not exist; an admin must create it` if the role is missing (never running
  `CREATE ROLE`), so the deploy identity never holds `CREATEROLE`. Every migration step runs without
  it (tested live).
- Objects the deploy identity creates (view, schema) can only be dropped by that identity, so keep one
  deploy identity for the life of the tables.
