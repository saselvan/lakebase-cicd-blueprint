# Lakebase CI/CD — Your Questions Answered

Point-by-point answers, mapped to a working reference we built and ran end-to-end on Lakebase
Autoscaling projects (PostgreSQL 16, Databricks Terraform provider v1.132.0): Terraform created the
synced table, it reached ONLINE, Liquibase applied the app role, grants, and indexes, the app role
could read, and a copy-on-write branch test left production untouched.

Lakebase is the Autoscaling **projects** model (`databricks postgres` CLI / `databricks_postgres_*`
Terraform). Standalone database instances are legacy/KTLO — build on projects.

---

## 1. GitHub + Terraform for static resources; Airflow for jobs; Liquibase for DB — chaining Liquibase into the Terraform deploy for permissions and indexes.

Your split is the right division of labor:

| Layer | Owns |
|---|---|
| Terraform | project, branches, catalogs, synced tables, roles |
| Liquibase | schemas, GRANTs, indexes, functions, views |
| Airflow / GH Actions | sequence: apply → wait for sync → migrate |

Your Airflow `SubmitRunOperator` pattern fits: after Terraform, a sensor waits for the synced table
to reach `ONLINE`, then triggers the Liquibase run.

## 2. Manage sync tables + metadata catalogs via Terraform; then start a Liquibase pipeline for permissions and indexes.

Create and load the synced table with the `databricks_postgres_synced_table` Terraform resource. It
works against an existing standard UC catalog — no dedicated catalog registration required. If you
do want a dedicated registered catalog (`databricks_postgres_catalog`), that needs the `CREATE
CATALOG` privilege on your metastore.

## 3. Even as superuser I can't modify default permissions on tables the databricks_writer account creates. I'd love to tie into that run to deploy security/indexes as it runs.

You are correct, and it is by design:

- `GRANT databricks_writer_<dbid> TO CURRENT_USER` → denied
- `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` → denied

The supported alternative: the deploy identity applies **explicit** grants on the writer-owned table:

```sql
GRANT USAGE  ON SCHEMA <schema>          TO <app_role>;
GRANT SELECT ON TABLE  <schema>.<table>  TO <app_role>;
```

Put these in a Liquibase changeset with `runOnChange:true` that runs right after the sync completes,
so the pipeline applies them automatically each run. The deploy identity needs the required database
privileges; managing indexes or dropping/replacing a synced table may also require synced-table
manager authorization.

## 4. Mirror UC structure into Postgres; app-specific permissions on common tables. We fragmented into separate DBs per app because we couldn't set default perms — and still ended up assigning permissions manually anyway.

You don't need separate databases. Use roles + explicit grants, reapplied by the pipeline:

- **App-owned tables** (you create them): schema roles work fully, including `ALTER DEFAULT
  PRIVILEGES`, because you own them. Use a `_ro` / `_rw` / `_admin` role per schema.
- **Synced tables** (writer-owned): explicit `GRANT SELECT` per table/schema, reapplied post-sync.

One database, app-specific roles, grants as code — consolidate back to the mirrored single-database
design.

## 5. Looked at ariga/atlas for security-as-code, decided it wasn't worth the price.

You don't need it. The Liquibase changesets above are your security-as-code at no extra cost.

## 6. Indexes: on large initial loads I want the load to finish before indexing. Two PRs work, but if we drop/move the table I can't easily rerun and split the two pieces, and people forget the second PR.

Load-first-then-index works, and one pipeline replaces two PRs: Terraform creates the synced table →
wait for the initial load to reach `ONLINE` → Liquibase builds the indexes. No forgotten second PR.

Make index DDL idempotent (`CREATE INDEX IF NOT EXISTS`); use `CREATE INDEX CONCURRENTLY` on a large
live table so reads don't block. Drop indexes for a big load only — on steady-state incremental
syncs, keep them.

## 7. Primary goal: never drop permissions or tables, so downstream apps never lose access — even if data is slightly stale.

A full refresh keeps the previous copy until the new sync completes, so a routine re-sync does not
drop the table, and grants and indexes persist. The pipeline reapplies the explicit grants every
run, so access is restored after a replace, and risky drop/move changes are rehearsed on a
copy-on-write branch first (we dropped an index on a branch and the production copy kept its
indexes).

One caveat to be straight about: deleting or replacing a synced table does drop the corresponding
Postgres table, so a replacement can cause a brief availability or permission gap — don't promise
downstream apps zero-downtime through a table replacement.

---

## In short

- Your architecture is sound; the split and tooling match what we'd advise.
- The one real constraint is the synced-table permission model: you cannot set the writer role's
  default privileges. The answer is explicit grants reapplied by the pipeline.
- Two concerns, two tools: load speed → load-then-index; risky rebuild/move → branch-test. Don't use
  one to solve the other.
