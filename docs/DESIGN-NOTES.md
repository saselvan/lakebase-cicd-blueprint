# Lakebase CI/CD — Design Notes

A recommended approach to CI/CD for Lakebase synced tables, with the reasoning behind each choice.
Exercised on real Lakebase synced tables on the Autoscaling **projects** model (PostgreSQL 16,
Databricks CLI v1.17.0 / v1.19.0, Terraform v1.16.2, Databricks Terraform provider v1.132.0,
Liquibase 4.33.0): live on Lakebase, one 3-row synced table (`members`) was exercised end to end
through both the DABs path (2026-10-15) and the Terraform + Liquibase path (2026-10-14) as a
non-superuser deploy service principal, and two tables (`members` and `providers` in one shared
schema, 16/16 verify checks) were verified live on a Lakebase child branch (`1-8 CU`, 2026-10-19).
Two shared-schema tables with a `view_name` override through Liquibase 4.33.0 and the DABs renderer
also run in the Docker PostgreSQL 16 proofs (`docker_multitable_proof.sh`,
`docker_double_apply_proof.sh`, `docker_view_column_change_proof.sh`). Terraform (or the DABs job)
created the synced tables; the deploy polled each table's status until its initial load finished
(`ONLINE`); the migration checked the admin-created app role and applied schema grants, indexes, and
a per-table consumer view; the app role could read through its view (and was denied on the base
table); and a copy-on-write branch test left production untouched.

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
cannot be joined, and its default privileges cannot be changed — not even as the project owner (a
member of `databricks_superuser`; Lakebase does not grant customers a Postgres superuser role). So
`ALTER DEFAULT PRIVILEGES` is a dead end:

- `GRANT databricks_writer_<dbid> TO CURRENT_USER` → denied
- `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` → denied

The supported path needs no superuser — use a consumer view:

1. The managed writer role owns the synced base table, and the identity that **creates** the synced
   table is granted `SELECT` on it — in fact `SELECT WITH GRANT OPTION`.
2. That same identity runs the migration, so it can `CREATE OR REPLACE VIEW` over the synced table.
3. Because it **owns the view**, it can grant consumers `SELECT` on the view. The app role gets
   **schema `USAGE` + `SELECT` on the view only** — never a grant on the base table. So "consumers
   read the view, never the base table" is literally enforced: verified live, the app role's
   `SELECT` on the base table is **denied** (`permission denied for table`), while
   `SET ROLE <app_role>; SELECT ... FROM <view>` returns rows — the view resolves as its owner (the
   deploy identity, which holds base `SELECT`), so consumers never need base access. **Do not set
   `security_invoker = true` on the consumer view**: this pattern relies on default PostgreSQL view
   behavior (views check the underlying table as the view owner); with `security_invoker = true`,
   PostgreSQL checks the base synced table as the calling `app_role`, which is denied (`verify`'s
   real-read check catches this and fails closed).

```sql
DROP VIEW IF EXISTS <schema>.<table>_v;
CREATE OR REPLACE VIEW <schema>.<table>_v AS SELECT * FROM <schema>.<table>;
GRANT USAGE  ON SCHEMA <schema>            TO <app_role>;
GRANT SELECT ON <schema>.<table>_v         TO <app_role>;
```

Both paths execute `DROP VIEW IF EXISTS`, `CREATE OR REPLACE VIEW`, and `GRANT SELECT` inside a
single database transaction (`autocommit=False` in `dabs/migration_job.py::apply_sql_to_lakebase`,
and one Liquibase changeset `004-app-view` with default `runInTransaction:true`). Dropping and
recreating the view in one transaction lets `SELECT *` survive base-table column drops, renames, and
reorders without opening a window where the view is missing or ungranted, while keeping the view in
place between deploys so an unplanned synced-table replace still fails closed.

Keep the creator and the view owner the same identity and no `databricks_superuser` is needed.
A view needs only `SELECT` on the synced table, which its creator is granted. Granting the table itself to the app would also need the grant option.

**App roles need `CREATEROLE` once.** Every migration step runs as a deploy identity with no
superuser and no `CREATEROLE` (tested live). See README "Who does what". An admin creates each
`NOLOGIN` app role once (`CREATE ROLE <app_role> NOLOGIN`) before the first deploy; changeset
`001-app-role` (`role_guard_sql`) checks `pg_roles` and fails loudly with
`app role <name> does not exist; an admin must create it` if the role is missing (never running
`CREATE ROLE`), so the deploy identity never needs `CREATEROLE`. Keep one deploy identity for the
life of the tables: it owns the view and schema it creates, and no other identity can drop them.

### Row-level filtering (RLS is off the table — use the view)

Row-level security policies **cannot be applied to a synced table** (an owner-only operation on the
writer-owned table). Since consumers already read through a view, do row filtering **in the view**:
give each consumer group its own view with a `WHERE` clause, and grant that group `SELECT` on only
its view (pattern — not tested in this repo; the single-view `WHERE` edit is tested in
`docker_view_edit_proof.sh`).

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

Make index DDL idempotent (`CREATE INDEX IF NOT EXISTS`). This reference intentionally does **not**
use `CREATE INDEX CONCURRENTLY`: on the DABs path, `apply_sql_to_lakebase` runs all statements for
the deploy in one non-autocommit `psycopg` transaction (`autocommit=False`), and on the Liquibase
path each changeset (`001`, `002`, `003-index-<col>`, `004`) runs in its own transaction
(`runInTransaction:true` by default) — neither allows `CREATE INDEX CONCURRENTLY`, so both emit
plain `CREATE INDEX IF NOT EXISTS`. If you need a non-blocking build on
a large live table, run that `CREATE INDEX CONCURRENTLY` as a separate, outside-transaction step of
your own. A Liquibase changeset is tracked in `DATABASECHANGELOG` and runs once — but see the next
section for why the index changeset should be `runAlways:true` so it rebuilds after a table replace.

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
  - This self-heal assumes the replace itself **succeeds** — and why a **primary-key replace**
    differs from an **in-place sync-mode change** matters here. On a primary-key change (tested live
    2026-10-15 on the DABs path), dropping the consumer view first allowed `bundle deploy` to
    recreate the synced table cleanly (`Recreated postgres_synced_tables.members`), after which the
    migration restored the view, grants, and indexes. By contrast, on an in-place sync-mode change
    (`scheduling_policy` transition), all six transitions tested with the view in place **wedged**
    the synced table: the platform `DELETE` returned an error and left the table in
    `SYNCED_TABLE_OFFLINE_FAILED`, a state a **redeploy cannot clear** (it matches a known platform
    incident), and in-place sync-mode replacement without the view was not re-tested after that
    wedge. The `runAlways` migration never gets a healthy table to reapply against, so a plain
    redeploy does **not** recover it — **open a support case**. (In the throwaway repro branch,
    recovery required deleting and recreating the branch; that is not an option on a real project.)
    The operational takeaway is simple: do **not** attempt an in-place sync-mode change. Use a
    **blue/green swap** instead — see "Changing sync mode: prefer a blue/green swap" below.

- **A replace is not zero-downtime.** Measured live (one 3-row table: drop view, `terraform apply
  -replace`, `deploy.sh`): the app could not read for about 50 s, from the view drop until the
  redeploy put it back. Terraform destroy + create took about 15 s; the initial sync plus Liquibase
  took the rest, so the gap grows with table size.

A sync-mode change is a delete + recreate: the platform cannot change a synced table's sync mode in
place through any interface, so Terraform plans a replace. Treat a sync-mode change as a planned rebuild, not a live toggle — and pick Triggered/Continuous at
create time if row-incremental is the goal, since flipping mode on a live table forces the replace above.

### Changing sync mode: prefer a blue/green swap

Because a mode change forces a full replace of the *same* synced table, the safest path is to stand up
a new table beside the old one rather than replace in place (recommended design pattern — not tested
end to end as a live mode-swap run; the `view_name` override and duplicate-view guard it relies on
are tested in Docker PG16 and unit tests):

1. Add a **new** entry to `config/tables.json` with a **new `synced_table_id`** and the target
   `scheduling_policy` — leave the existing table running.
2. Deploy; wait for the new table to reach `ONLINE`; run the migration so its role, grants, indexes,
   and consumer view are in place.
3. Cut consumers over to the new table (or its view).
4. Remove the old entry and deploy again to drop the old table — during a maintenance window.

This never deletes a live table, so there is no availability gap. Blue/green is the **only
recommended path for a sync-mode change**: an in-place replace of a table that has a consumer view
was observed to **wedge** the synced table (`SYNCED_TABLE_OFFLINE_FAILED`) with no redeploy recovery,
and dropping the consumer view first is **not** a safe workaround — it destroys the view's grants,
opens an availability gap, and still risks the wedge.

Two operational notes for the swap:

- The **old** config entry must be given a **different `view_name`** (or be removed) **before** the
  new entry takes over the stable view name — otherwise both entries resolve to the same consumer
  view name. The generator now **fails loudly** when two entries resolve to the same consumer view
  name.
- **Nothing in either path drops the old view.** Retiring the old synced table and its view is a
  manual step (step 4 above), done in a maintenance window.

## Branch-per-PR (ephemeral test environments)

Use copy-on-write branches to give each change its own throwaway environment: branch off production,
run the migration and tests against the branch, tear it down when the PR merges. Verified live on the
Autoscaling projects model:

- A branch forks from production at a fixed point and comes up **ready in seconds**. A destructive
  change on the branch (e.g. dropping an index) is **invisible to production** — production kept all
  its indexes and rows throughout. Teardown is seconds and leaves only production.
- **Each branch has its own connection endpoint** — resolve the branch's endpoint (list the project's
  branch endpoints) rather than reusing the production host. This is the main gotcha for scripts/CI.
  Both paths (`dabs/migration_job.py` and `scripts/resolve_endpoint.sh`) require an endpoint with
  `status.endpoint_type` matching `READ_WRITE`, a ready connection `status.hosts.host`, and a
  non-empty resource `name` before minting a credential. Neither path falls back to a `READ_ONLY`
  endpoint: migrations execute DDL and post-migration `verify` checks validate the target branch's
  primary compute, so connecting to a read-only replica would fail late (`read-only transaction`) or
  test the wrong endpoint.
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
needing a different index shape extends the shared SQL helpers in `dabs/render_ddl.py` (or
`liquibase/generate_changelogs.py`) and regenerates `liquibase/generated/*.changelog.sql` and
`dabs/resources/*.yml` (never hand-edit the generated files). Everything else is data in
`config/tables.json`.

## The DABs renderer (Python teams)

The DABs path emits its migration SQL from a small Python renderer (`dabs/render_ddl.py`) that reads
the same `config/tables.json`, so Python shops can adopt the pattern with no Java/Liquibase runtime.
It is **not** a migration framework and keeps **no version table**: it emits **idempotent** SQL (a
`pg_roles` existence check requiring an admin-created role, `GRANT`, `CREATE INDEX IF NOT EXISTS`,
`DROP VIEW IF EXISTS` + `CREATE OR REPLACE VIEW`) that the Workflow job applies **on every deploy**, and that `python -m dabs.render_ddl | psql`
applies outside the job. Because there is no version-tracking state, re-applying is a clean
reconciling no-op — never a duplicate-key rollback on a second apply. That is what lets access
self-heal after a synced-table replace, exactly like Liquibase `runAlways:true`. The same caveat
applies: this self-heal assumes the replace itself succeeds — and an in-place sync-mode change on a
table that has a consumer view can **wedge** it (see the caveat above), which a redeploy cannot
recover. Use a **blue/green swap** for a sync-mode change — see "Changing sync mode: prefer a
blue/green swap" above.

The renderer is the **single source of the idempotent SQL**: the Liquibase generator
(`liquibase/generate_changelogs.py`) imports the same four helpers (`role_guard_sql`,
`grant_statements`, `index_statements`, `view_statements`) and the same `validate_identifier`, then
wraps their statements in per-table changesets. So both engines emit the same object DDL from one
definition. Object names come from the config; index columns are the one table-specific spot, as
with the Liquibase path.

(Why no Alembic: an earlier variant rendered from Alembic and could roll the whole transaction back
on a second apply because of its `alembic_version` bookkeeping, so dropping the framework removes
that class of bug — see [ADR 0006](adr/0006-dabs-variant-mechanics.md) for the full rationale.)

## Validated live 2026-10-14 (deploy as a service principal)

Run on a throwaway branch with a non-superuser deploy SP (no `CREATEROLE`), one 3-row SNAPSHOT table.

- S1, full flow as the deploy SP: PASS. 1 added, ONLINE in about 20 s, Liquibase ran 5, verify (5/5 checks in that 2026-10-14 run; verify now runs 8 checks per table); the SP owns the schema, view and Liquibase tables.
- S2, break and heal: PASS when the deploy SP revokes; the project owner's identical `REVOKE` changed nothing (see RUNBOOK → "Testing drift").
- S3, second deploy identity: PASS (fails loudly). Another SP and the project owner get `permission denied` / `must be owner`; its deploy fails at Terraform and Liquibase.
- S4, rotating the deploy SP: `REASSIGN OWNED` alone fails on the index changeset; rebuilding the synced table as the new SP works (see RUNBOOK → "Rotating the deploy service principal").
- S5, app role: PASS. Reads 3 rows through the view, denied the base table.

## Key decisions at a glance

- Provision with Terraform; apply the DB layer with Liquibase; sequence with a wait-for-`ONLINE` gate.
- Grant app access through a deploy-identity-owned **view**, not the writer-owned base table — no superuser.
- Keep grant / index / view changesets `runAlways:true` so access self-heals after a table replace.
- Choose one-database-with-roles vs database-per-app on isolation needs, not on the permission limit.
