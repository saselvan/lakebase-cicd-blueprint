# Lakebase CI/CD — Design Notes

A recommended approach to CI/CD for Lakebase synced tables, with the reasoning behind each choice.
Verified end-to-end on the Autoscaling **projects** model (PostgreSQL 16, Databricks Terraform
provider v1.132.0): Terraform created the synced table; the deploy polled the table's status until
its initial load finished (`ONLINE`); Liquibase then applied the app role, grants, indexes, and a
consumer view; the app role could read; and a copy-on-write branch test left production untouched.

Build on the Autoscaling projects model (`databricks postgres` CLI / `databricks_postgres_*`
Terraform); standalone database instances are legacy.

---

## Division of labor

Keep each tool to what it owns:

| Layer | Owns |
|---|---|
| Terraform | project, branches, catalogs, synced tables, roles |
| Liquibase | schemas, GRANTs, indexes, functions, views |
| Orchestrator (Airflow / GitHub Actions) | sequence: apply → wait for sync → migrate |

An Airflow `SubmitRunOperator` (or a GitHub Actions job) fits cleanly: after Terraform, a sensor
waits for the synced table to reach `ONLINE`, then triggers the Liquibase run.

## Provisioning synced tables, and the ONLINE gate

Create and load the synced table with the `databricks_postgres_synced_table` resource. It works
against an existing standard UC catalog — no dedicated catalog registration required. A dedicated
registered catalog (`databricks_postgres_catalog`) needs `CREATE CATALOG` on the metastore.

`terraform apply` does **not** block until the table is `ONLINE` — it returns once the synced table
is created, while the initial load runs asynchronously. So a wait-for-`ONLINE` gate
(`scripts/wait_for_sync.sh`) before indexing is **required**, not optional — otherwise indexes try to
build on an empty or partial table.

## Permissions: the consumer-view pattern (no superuser)

Synced tables are created and owned by a managed writer role (`databricks_writer_<dbid>`). That role
cannot be joined, and its default privileges cannot be changed — not even by a superuser. So
`ALTER DEFAULT PRIVILEGES` is a dead end:

- `GRANT databricks_writer_<dbid> TO CURRENT_USER` → denied
- `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` → denied

The supported path needs no superuser — use a consumer view:

1. The identity that **creates** the synced table automatically owns it and is granted `SELECT`.
2. That same identity runs Liquibase, so it can `CREATE OR REPLACE VIEW` over the synced table.
3. Because it **owns the view**, it can grant consumers `SELECT` on the view. Consumers read the
   view, never the base table.

```sql
CREATE OR REPLACE VIEW <schema>.<table>_v AS SELECT * FROM <schema>.<table>;
GRANT USAGE  ON SCHEMA <schema>            TO <app_role>;
GRANT SELECT ON <schema>.<table>_v         TO <app_role>;
```

Keep the creator and the view owner the same identity and no `databricks_superuser` is needed. (A
direct `GRANT SELECT` on the writer-owned base table is possible, but granting it to a *different*
identity is the superuser path — the view avoids it.)

## One database vs a database per app

The permission limit above is not a reason to split into a database per app. Roles plus explicit
grants (and the consumer view) cover app-specific access in one database:

- **App-owned tables** (created by the app): schema roles work fully, including `ALTER DEFAULT
  PRIVILEGES`. A `_ro` / `_rw` / `_admin` role per schema is a clean layout.
- **Synced tables** (writer-owned): a consumer view + `GRANT SELECT` on the view.

A database-per-app is still a legitimate choice for *isolation* — smaller blast radius, a hard
security boundary, independent backup/restore. Decide by which of those matters: if the split was
only to work around the permission limit, one database with roles and grants-as-code is simpler; if
it's for isolation, keep it. The point is to choose deliberately, not by default.

## Indexes: load-then-index, idempotent

Build indexes *after* the initial load, in one pipeline: Terraform creates the synced table → wait
for `ONLINE` → Liquibase builds the indexes. One pipeline, no forgotten second PR.

Make index DDL idempotent (`CREATE INDEX IF NOT EXISTS`) and use `CREATE INDEX CONCURRENTLY` on a
large live table so reads don't block. A Liquibase changeset is tracked in `DATABASECHANGELOG` and
runs once — but see the next section for why the index changeset should be `runAlways:true` so it
rebuilds after a table replace.

## Keeping access through refreshes and rebuilds

Verified across all three sync modes (Snapshot, Triggered, Continuous):

- A **routine same-mode refresh** does not drop the table — indexes and grants persist, and Liquibase
  is never re-triggered by the sync.
- **Changing a table's sync mode forces a full replacement** (all six directed transitions confirmed:
  destroy + recreate), which drops custom indexes and grants down to the primary key.
- For a redeploy to **restore** those after a replacement, the grant / index / view changesets must be
  **`runAlways:true`**. With `runOnChange`, the redeploy sees unchanged checksums, skips, and leaves
  the app without access. `runAlways` + idempotent SQL (`GRANT`, `CREATE INDEX IF NOT EXISTS`,
  `CREATE OR REPLACE VIEW`) is what keeps access intact after a rebuild.

Treat a sync-mode change as a planned rebuild, not a live toggle — and pick Triggered/Continuous at
create time if row-incremental is the goal, since flipping mode on a live table forces the replace above.

## Testing risky changes

Use copy-on-write branches to rehearse destructive migrations (dropping an index, moving a table) on
an ephemeral branch; the production copy is untouched. Two concerns, two tools: load speed →
load-then-index; risky rebuild/move → branch test. Don't use one to solve the other.

## Managing many tables

Scale to many synced tables from **one config file**, not by copy-pasting resources or scripts.
`config/tables.json` is the single source of truth, read by both layers:

- **Terraform** does `for_each` over `jsondecode(file(".../config/tables.json"))`, so one
  `databricks_postgres_synced_table` block provisions every entry. Adding a table is a one-line
  edit to the JSON — no new resource, no new variable.
- **The deploy loop** (`scripts/deploy.sh`) reads the same file, runs `terraform apply` once, then
  iterates the entries: wait-for-`ONLINE` → Liquibase migrate (passing per-table `synced_table`,
  `app_schema`, `app_role`, and index columns) → verify.

The Liquibase changesets are already parametrized (`${synced_table}`, `${app_schema}`,
`${app_role}`), so the same changelog serves every table. **Index columns are the one inherently
table-specific customization point** — the two-index template in `003-indexes.sql` covers the
common case (`${index_col_1}`/`${index_col_2}` from each entry's `index_columns`), and a table
needing a different index shape (more indexes, composite/partial, or a different type) edits that
changeset directly. Everything else is data in `config/tables.json`.

## Key decisions at a glance

- Provision with Terraform; apply the DB layer with Liquibase; sequence with a wait-for-`ONLINE` gate.
- Grant app access through a deploy-identity-owned **view**, not the writer-owned base table — no superuser.
- Keep grant / index / view changesets `runAlways:true` so access self-heals after a table replace.
- Choose one-database-with-roles vs database-per-app on isolation needs, not on the permission limit.
