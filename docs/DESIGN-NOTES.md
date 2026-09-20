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

### Row-level filtering (RLS is off the table — use the view)

Row-level security policies **cannot be applied to a synced table** (an owner-only operation on the
writer-owned table). Since consumers already read through a view, do row filtering **in the view**:
give each consumer group its own view with a `WHERE` clause, and grant that group `SELECT` on only
its view.

```sql
-- Example: a plan-scoped view for one consumer group
CREATE OR REPLACE VIEW cicd_proj.members_gold_v AS
  SELECT * FROM cicd_proj.members WHERE plan_code = 'GOLD';
GRANT SELECT ON cicd_proj.members_gold_v TO gold_app_ro;
```

It's not policy-based RLS, but it achieves per-consumer row scoping with the same no-superuser,
view-owned-by-deploy-identity model. Keep these views in a `runAlways:true` changeset so they
survive a table replacement like the others.

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
  - This self-heal assumes the replace itself **succeeds**. When a dependent consumer view exists,
    an in-place replace is **blocked**: the replace drops the base table, and Postgres will not drop a
    table that still has a dependent view (non-`CASCADE` dependency), so the drop fails, the recreate
    never happens, and the `runAlways` migration never runs to restore access. A plain redeploy does
    **not** self-heal in that case — the view must be dropped first, or the swap done side-by-side.
    See "Changing sync mode: prefer a blue/green swap" below.

Treat a sync-mode change as a planned rebuild, not a live toggle — and pick Triggered/Continuous at
create time if row-incremental is the goal, since flipping mode on a live table forces the replace above.

### Changing sync mode: prefer a blue/green swap

Because a mode change forces a full replace of the *same* synced table, the safest path is to stand up
a new table beside the old one rather than replace in place:

1. Add a **new** entry to `config/tables.json` with a **new `synced_table_id`** and the target
   `scheduling_policy` — leave the existing table running.
2. Deploy; wait for the new table to reach `ONLINE`; run the migration so its role, grants, indexes,
   and consumer view are in place.
3. Cut consumers over to the new table (or its view).
4. Remove the old entry and deploy again to drop the old table — during a maintenance window.

This never deletes a live table, so there is no availability gap. If you instead replace in place,
**drop the consumer view first**: the replace deletes the base table, and Postgres will not drop a
table that still has a dependent view, so the view must be removed before the replace and recreated by
the `runAlways` migration afterward. Blue/green avoids that dependency step entirely.

## Branch-per-PR (ephemeral test environments)

Use copy-on-write branches to give each change its own throwaway environment: branch off production,
run the migration and tests against the branch, tear it down when the PR merges. Verified live on the
Autoscaling projects model:

- A branch forks from production at a fixed point and comes up **ready in seconds**. A destructive
  change on the branch (e.g. dropping an index) is **invisible to production** — production kept all
  its indexes and rows throughout. Teardown is seconds and leaves only production.
- **Each branch has its own connection endpoint** — resolve the branch's endpoint (list the project's
  branch endpoints) rather than reusing the production host. This is the main gotcha for scripts/CI.
- Branches take an **expiry (TTL) at create time** and auto-clean at the TTL even without an explicit
  delete — a safety net against orphaned branches.
- The database credential is **minted at runtime** (short-lived OAuth), never stored.

`scripts/branch_test.sh` runs this lifecycle from an authenticated machine.

### Automating it in CI (the included workflows are reference-only)

The `pr-validate` / `pr-cleanup` workflows show the intended shape — create a branch per PR, deploy and
test on it, tear it down on close — but they are **reference-only**. To run them live you need two
things the reference can't assume: a CI runner that can actually reach your workspace (public
GitHub-hosted runners are commonly blocked by a workspace IP ACL, so a self-hosted / allowlisted runner
is typical), and a no-secret auth path (OIDC / Workload Identity Federation), which a workspace or
account admin sets up. Wire them to your own workspace deliberately.

Two concerns, two tools: load speed → load-then-index; risky rebuild/move → branch test. Don't use one
to solve the other.

## Managing many tables

Scale to many synced tables from **one config file**, not by copy-pasting resources or scripts.
`config/tables.json` is the single source of truth, read by both layers:

- **Terraform** does `for_each` over `jsondecode(file(".../config/tables.json"))`, so one
  `databricks_postgres_synced_table` block provisions every entry. Adding a table is a one-line
  edit to the JSON — no new resource, no new variable.
- **The deploy loop** (`scripts/deploy.sh`) reads the same file, runs `terraform apply` once,
  generates one Liquibase changelog per table (`liquibase/generate_changelogs.py`), then iterates
  the entries: wait-for-`ONLINE` → `liquibase update` against that table's OWN changelog → verify.

Each table gets its OWN generated changelog (`liquibase/generated/<name>.changelog.sql`) with
role/schema/table and index columns BAKED in — no `${…}` property substitution. This matters:
Liquibase keys a changeset by (FILENAME, id, author) and folds substituted property values into the
checksum, so the earlier shared-changelog-run-per-table design collided when two tables shared one
`app_schema` (one `DATABASECHANGELOG`, second table's `001-app-role` failed its checksum and its role
was never created). A distinct changelog FILE per table makes each changeset identity distinct, so
shared-schema tables coexist. **Index columns are the one inherently table-specific spot** — the
generator emits one `003-index-<col>` changeset per `index_columns` entry (0/1/N, no cap); a table
needing a different index shape edits its generated changelog. Everything else is data in
`config/tables.json`.

## The DABs renderer (Python teams)

The DABs path emits its migration SQL from a small Python renderer (`dabs/render_ddl.py`) that reads
the same `config/tables.json`, so Python shops can adopt the pattern with no Java/Liquibase runtime.
It is **not** a migration framework and keeps **no version table**: it emits **idempotent** SQL (a
`pg_roles` guard around `CREATE ROLE`, `GRANT`, `CREATE INDEX IF NOT EXISTS`, `CREATE OR REPLACE
VIEW`) that the Workflow job applies **on every deploy**, and that `python -m dabs.render_ddl | psql`
applies outside the job. Because there is no version-tracking state, re-applying is a clean
reconciling no-op — never a duplicate-key rollback on a second apply. That is what lets access
self-heal after a synced-table replace, exactly like Liquibase `runAlways:true`.

The renderer is the **single source of the idempotent SQL**: the Liquibase generator
(`liquibase/generate_changelogs.py`) imports the same four helpers (`role_guard_sql`,
`grant_statements`, `index_statements`, `view_statements`) and the same `validate_identifier`, then
wraps their statements in per-table changesets. So both engines emit the same object DDL from one
definition. Object names come from the config; index columns are the one table-specific spot, as
with the Liquibase path.

(Why no Alembic: an earlier variant rendered from Alembic. `alembic upgrade head --sql` prepends an
unguarded `alembic_version` create + `INSERT`, and once a second revision existed the reconcile
tripped a duplicate-key on `alembic_version` on the second apply and rolled the whole transaction
back — the object DDL never reconciled. Removing the framework removes that class of bug.)

## Key decisions at a glance

- Provision with Terraform; apply the DB layer with Liquibase; sequence with a wait-for-`ONLINE` gate.
- Grant app access through a deploy-identity-owned **view**, not the writer-owned base table — no superuser.
- Keep grant / index / view changesets `runAlways:true` so access self-heals after a table replace.
- Choose one-database-with-roles vs database-per-app on isolation needs, not on the permission limit.
