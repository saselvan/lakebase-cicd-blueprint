# RUNBOOK — talk to this pipeline confidently

A step-by-step walkthrough of the reference pipeline, so you can run it and explain each stage.

## The one-sentence story

**Terraform builds the infrastructure (the synced table), we wait for the first load to finish, then
Liquibase applies the database objects (role, grants, indexes, consumer view) — and it's all
idempotent, so grants are reapplied every run and access is restored after a table replace.** (A
drop/replace still has a brief gap — it is not zero-downtime; see `docs/DESIGN-NOTES.md` → "Keeping access through refreshes and rebuilds".)

## Who does what

| Tool | Job | Files |
|---|---|---|
| Terraform | create/own the synced table (Delta → Lakebase Postgres) | `terraform/` |
| Liquibase | app role, explicit grants, indexes, consumer view (one generated changelog per table) | `liquibase/generated/` (from `liquibase/generate_changelogs.py`) |
| Orchestration | run them in the right order, wait for the sync | `scripts/deploy.sh` |
| GitHub Actions | run that same sequence on push / PR | `.github/workflows/` |

## Run it yourself (the exact commands, in order)

```bash
# authenticate the Databricks CLI to your workspace first, then:
export PROFILE=<your-cli-profile>
export WAREHOUSE_ID=<your-sql-warehouse-id>
export HOST=<your-lakebase-rw-endpoint-host>
export PGUSER=<your-databricks-username>   # your login email

# 0. one-time: create the demo Delta source table the sync reads from
./scripts/seed_source.sh

# 1-4. the whole pipeline (what CI runs), locally:
./scripts/deploy.sh
```

`deploy.sh` does, in order:
1. `terraform apply` — creates the synced table via `databricks_postgres_synced_table`. Reruns are a
   no-op because Terraform owns the state.
2. `scripts/wait_for_sync.sh` — polls `databricks postgres get-synced-table` until `detailed_state`
   contains `ONLINE`. This is what guarantees indexes go on **after** the load, not during it.
3. generate per-table changelogs (`liquibase/generate_changelogs.py`), then `liquibase update`
   against each table's OWN changelog (`generated/<name>.changelog.sql`): create role → grant
   USAGE/SELECT → create indexes (one per `index_columns` entry) → create the consumer view + grant.
   A distinct changelog file per table gives each changeset a distinct identity, so shared-schema
   tables never collide in `DATABASECHANGELOG`. Reruns reapply the `runAlways` changesets.
4. verify — `has_table_privilege(app_role, ...) = t` and the indexes exist.

## The PR flow (branching)

- Open a PR → `pr-validate.yml` creates an **ephemeral copy-on-write branch** `pr-<n>`, runs the
  migrations against **that branch** (never production), validates. Dropping an index on a branch
  leaves production untouched.
- Close the PR → `pr-cleanup.yml` deletes the branch (and the TTL would anyway).
- Do this by hand with `scripts/branch_test.sh`.

## Why it solves each concern (short)

- **"Can't set default perms on writer-owned tables"** → true, by design. We use **explicit**
  grants, reapplied every run, and a consumer **view** owned by the deploy identity — no superuser
  needed.
- **"Index after load, no second PR"** → one pipeline: create → wait → index. Idempotent.
- **"Never lose access"** → re-sync doesn't drop the table; grants reapply and self-heal even after
  a replace; risky changes get tested on a branch first.

## Gotchas we already hit (so you're not surprised)

1. **`spec` is an attribute, not a block** in the Terraform provider: `spec = { ... }`, not `spec { }`.
2. **Liquibase splits on `;`** — a `DO $$ ... $$` block needs `splitStatements:false` on the changeset.
3. **`ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` is denied** even as a superuser.
   Don't try it. Use explicit grants (and a consumer view) instead.
4. **`CREATE CATALOG` needs the privilege on your metastore** — if you can't register a dedicated
   database catalog, target an existing standard UC catalog instead. Both work with the synced-table
   resource.
5. **Synced-table UC name is 3-part; the Postgres object is the last part** (`...cicd_proj.members`
   → Postgres `cicd_proj.members`).
6. **Branches need an expiry** (`ttl` / `no_expiry`) at create time, and auto-get a `primary` endpoint.
7. **Sync-ready signal** = `databricks postgres get-synced-table synced_tables/<cat.schema.table>`
   → `status.detailed_state` contains `ONLINE` (used by `wait_for_sync.sh`).

## Project facts

- Lakebase here is the **Autoscaling projects** model (`databricks postgres ...`), not standalone
  database instances. Terraform resource: `databricks_postgres_synced_table`.
- Find the writer role id (used in the writer role name) on your instance:
  `SELECT rolname FROM pg_roles WHERE rolname LIKE 'databricks_writer_%';`

## CI auth — the secure way (READ before wiring GitHub)

**Most secure = OIDC / Workload Identity Federation → NO secret stored anywhere.** GitHub Actions
mints a short-lived token per run; Databricks trusts it via a federation policy scoped to this repo.
Databricks officially supports this for GitHub Actions.

Prefer this over storing a long-lived client secret in GitHub. The database password is always
minted at runtime (`generate-database-credential`, ~1h TTL) — never stored. Two things to plan for:

1. **Network access:** the runner must be able to reach your workspace. If your workspace enforces
   IP access lists, use a runner inside an allowed network (self-hosted / managed) or add an
   exception for the runner's egress.
2. **Federation policy** is typically account-level and created by an account admin on a service
   principal. Set it up once, scoped to this repository.

Until CI is wired, run `scripts/deploy.sh` locally from an authenticated machine — it is the exact
same sequence.
