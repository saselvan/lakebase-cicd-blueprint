# Lakebase CI/CD — Questions Answered

Point-by-point answers, mapped to this working reference and verified end-to-end on Lakebase
Autoscaling projects (PostgreSQL 16, Databricks Terraform provider v1.132.0): Terraform created the
synced table, the pipeline waited for `ONLINE`, Liquibase applied the app role, grants, indexes, and
a consumer view, the app role could read, and a copy-on-write branch test left production untouched.

Lakebase here is the Autoscaling **projects** model (`databricks postgres` CLI / `databricks_postgres_*`
Terraform). Standalone database instances are legacy — build on projects.

---

## 1. GitHub + Terraform for static resources; Airflow for jobs; Liquibase for DB — chaining Liquibase into the Terraform deploy for permissions and indexes.

This split is a sound division of labor:

| Layer | Owns |
|---|---|
| Terraform | project, branches, catalogs, synced tables, roles |
| Liquibase | schemas, GRANTs, indexes, functions, views |
| Airflow / GitHub Actions | sequence: apply → wait for sync → migrate |

An Airflow `SubmitRunOperator` pattern fits well: after Terraform, a sensor waits for the synced
table to reach `ONLINE`, then triggers the Liquibase run.

## 2. Manage synced tables + catalogs via Terraform; then a Liquibase pipeline for permissions and indexes.

Create and load the synced table with the `databricks_postgres_synced_table` resource. It works
against an existing standard UC catalog — no dedicated catalog registration required. A dedicated
registered catalog (`databricks_postgres_catalog`) needs the `CREATE CATALOG` privilege on the metastore.

**Important, verified:** `terraform apply` does **not** block until the table is `ONLINE` — it
returns once the synced table is created, while the initial load runs asynchronously. So a
wait-for-`ONLINE` gate (`scripts/wait_for_sync.sh`) before indexing is **required**, not optional.

## 3. Even as superuser I can't modify default permissions on tables the databricks_writer account creates.

Correct, and it is by design (a security boundary — the pipeline owns the writer role):

- `GRANT databricks_writer_<dbid> TO CURRENT_USER` → denied
- `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` → denied

**The supported path avoids a superuser entirely — use a consumer view:**

1. The identity that **creates** the synced table automatically owns it and is granted `SELECT`.
2. That same identity runs Liquibase, so it can `CREATE OR REPLACE VIEW` over the synced table.
3. Because it **owns the view**, it can grant consumers `SELECT` on the view. Consumers read the
   view, never the base table.

```sql
CREATE OR REPLACE VIEW <schema>.<table>_v AS SELECT * FROM <schema>.<table>;
GRANT USAGE  ON SCHEMA <schema>            TO <app_role>;
GRANT SELECT ON <schema>.<table>_v         TO <app_role>;
```

Keep the **creator and the view owner the same identity** and no `databricks_superuser` is needed.
(A direct `GRANT SELECT` on the writer-owned base table is possible, but granting it to a *different*
identity is the superuser path — the view keeps you off it.) Put these in a changeset with
**`runAlways:true`** (see Q7 for why `runOnChange` is not enough).

## 4. Mirror UC structure into Postgres; app-specific permissions on common tables. We fragmented into separate DBs per app because we couldn't set default perms.

You don't *need* separate databases for the permission reason — roles + explicit grants (and the
consumer view above) cover it:

- **App-owned tables** (you create them): schema roles work fully, including `ALTER DEFAULT
  PRIVILEGES`, because you own them. A `_ro` / `_rw` / `_admin` role per schema is a clean layout.
- **Synced tables** (writer-owned): a consumer view + `GRANT SELECT` on the view.

That said, a database-per-app is still a legitimate choice for *isolation* — smaller blast radius, a
hard security boundary, independent backup/restore. So the question is which you're optimizing for:
if the split was only to work around the permission limit, one database with app-specific roles and
grants-as-code is simpler; if it's for genuine isolation, keep it. Worth deciding deliberately.

## 5. Looked at ariga/atlas for security-as-code, decided it wasn't worth the price.

The Liquibase changesets here are your security-as-code at no extra cost.

## 6. Indexes: on large initial loads I want the load to finish before indexing. Two PRs work, but people forget the second PR.

Load-first-then-index, and one pipeline replaces two PRs: Terraform creates the synced table → wait
for the initial load to reach `ONLINE` → Liquibase builds the indexes. No forgotten second PR.

A Liquibase changeset is tracked in `DATABASECHANGELOG`, so a plain index changeset runs once and
won't re-run on later deploys — but see Q7: after a table *replace* you want it to re-run, so mark it
`runAlways:true` with `CREATE INDEX IF NOT EXISTS` (idempotent no-op normally, rebuilds after a
replace). Use `CREATE INDEX CONCURRENTLY` on a large live table so reads don't block.

## 7. Primary goal: never drop permissions or tables, so downstream apps never lose access.

Here is what we verified live across all three sync modes (Snapshot, Triggered, Continuous):

- A **routine same-mode refresh** does not drop the table — indexes and grants persist, and Liquibase
  is never re-triggered by the sync.
- **Changing a table's sync mode forces a full replacement** (all six directed transitions confirmed:
  destroy + recreate), which drops custom indexes and grants down to the primary key.
- For a redeploy to **restore** those after a replacement, the grant/index/view changesets must be
  **`runAlways:true`**. With `runOnChange`, the redeploy sees unchanged checksums, skips, and leaves
  the app without access. `runAlways` + idempotent SQL (`GRANT`, `CREATE INDEX IF NOT EXISTS`,
  `CREATE OR REPLACE VIEW`) is what makes the "never lose access" goal actually hold.

Caveat, stated plainly: deleting or replacing a synced table does drop the corresponding Postgres
table, so a replacement causes a brief gap and requires the migrations to re-run. **Treat a sync-mode
change as a planned rebuild, not a live toggle** — and pick Triggered/Continuous at create time if you
want row-incremental (flipping mode on a live table forces the replace above).

---

## In short

- The architecture is sound; the split and tooling are the right shape.
- The real constraint is the synced-table permission model: you cannot set the writer role's default
  privileges. The answer is a consumer view the deploy identity owns — no superuser.
- Make the grant/index/view changesets `runAlways:true` so access self-heals after a table replace.
- Two concerns, two tools: load speed → load-then-index; risky rebuild/move → test on a branch. Don't
  use one to solve the other.
