# ADR 0001 — View-based, no-superuser access to synced tables

**Status:** Accepted

## Context
Synced tables are owned by an internal managed role (`databricks_writer_<dbid>`). You cannot join
that role or change its default privileges — not even as a superuser — so `ALTER DEFAULT PRIVILEGES`
can't grant app access to future synced objects. Granting a *different* identity direct access to the
base table requires `databricks_superuser`, which is `NOLOGIN` and not meant for automation.

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
