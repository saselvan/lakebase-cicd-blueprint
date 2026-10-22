# RUNBOOK — talk to this pipeline confidently

A step-by-step walkthrough of the reference pipeline, so you can run it and explain each stage.

## The one-sentence story

**Terraform builds the infrastructure (the synced table), we wait for the first load to finish, then
Liquibase applies the database objects (role, grants, indexes, consumer view) — and it's all
idempotent, so grants are reapplied every run and access is restored after a table replace.** (A
drop/replace still has a gap — it is not zero-downtime: measured live at about 50 s of app read outage on a 3-row table, mostly the initial sync, so it grows with table size; see `docs/DESIGN-NOTES.md` → "Keeping access through refreshes and rebuilds".)

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
export PROJECT=<your-lakebase-project-id>   # deploy is branch-driven; default my-lakebase-project
export BRANCH=<branch-to-deploy-to>         # default production
export PGUSER=<your-databricks-username>   # your login email; for a service principal, its application id

# 0. one-time: create the demo Delta source table the sync reads from
./scripts/seed_source.sh

# 1-4. the whole pipeline (what CI runs), locally:
./scripts/deploy.sh
```

`deploy.sh` does, in order (passing explicit `PROJECT`, `BRANCH`, and `PROFILE` settings via
`TF_VAR_*` and reading back `project_id`, `branch`, `databricks_profile`, and `logical_database`
from `terraform -chdir=terraform output -raw ...` after apply — aborting on any conflict between an
explicit caller setting and Terraform's resolved value; on no-apply paths, `resolve_endpoint.sh`
asks `terraform console`, which follows Terraform's variable precedence: `variables.tf` default →
`TF_VAR_logical_database` → `terraform.tfvars` → `terraform.tfvars.json` → `*.auto.tfvars` /
`*.auto.tfvars.json` in lexical order):
1. `terraform apply` — creates the synced table via `databricks_postgres_synced_table`. Reruns are a
   no-op because Terraform owns the state.
2. `scripts/wait_for_sync.sh` — polls `databricks postgres get-synced-table` until `detailed_state`
   contains `ONLINE` and not `FAILED` or `ERROR` (failure is checked first). This is what guarantees indexes go on **after** the load, not during it.
3. generate per-table changelogs (`liquibase/generate_changelogs.py`), then `liquibase update`
   against each table's OWN changelog (`generated/<name>.changelog.sql`): create role → grant
   USAGE/SELECT → create indexes (one per `index_columns` entry) → create the consumer view + grant.
   A distinct changelog file per table gives each changeset a distinct identity, so shared-schema
   tables never collide in `DATABASECHANGELOG`. Reruns reapply the `runAlways` changesets.
4. verify (`scripts/verify_table.sh`) — asserts the role and view exist, the app role has `SELECT`
   on the view and `USAGE` on the schema, a real read through the view works, the app role is denied
   on the base synced table by every route, and every `idx_<table>_<col>` exists; exits non-zero
   (failing the deploy) if not. Shares the check list in `dabs/checks.py` and is tested against a
   real Docker Postgres.

## The PR flow (branching)

- Open a PR → `pr-validate.yml` creates an **ephemeral copy-on-write branch** `pr-<n>`, runs the
  migrations against **that branch** (never production), and runs `verify_table_from_row` for every
  table after Liquibase so a missing grant, view, or index fails the PR check. Dropping an index on a
  branch leaves production untouched.
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
   → `status.detailed_state` contains `ONLINE` and not `FAILED` or `ERROR` (used by `wait_for_sync.sh`;
   `SYNCED_TABLE_ONLINE_PIPELINE_FAILED` fails the gate).
   A table in a failed state stays failed: a redeploy with no Terraform change starts no new sync,
   so every deploy fails at the gate until someone acts. Fix the cause, start a full refresh of the
   table's sync pipeline (`databricks pipelines start-update <pipeline_id> --full-refresh -p <profile>`;
   the pipeline id is in the synced table's status), then redeploy. Not tested in this repo.

## Testing drift (break and heal)

To prove verify catches drift, remove one grant **as the deploy service principal** (the identity
that made it):

```sql
REVOKE SELECT ON <schema>.<view> FROM <app_role>;
```

Verify then fails on one line (the app role's `SELECT` on the view), and a plain `deploy.sh` re-runs
the `runAlways` changesets and verify passes again. Run as the **project owner**, the same statement
returns `REVOKE` and changes nothing, with no warning: Postgres removes only grants the caller made,
and the deploy SP made this one.

## Rotating the deploy service principal

Plan it like a table replace: the app cannot read between steps 2 and 4, and the initial sync makes
that gap grow with table size. Do it **before** you delete the old SP.

`REASSIGN OWNED` alone is not enough. It moves the view, schema and Liquibase tables, but the new SP
still gets `[Databricks SyncedTable] permission denied for synced table operation` on the index
changeset (`CREATE INDEX IF NOT EXISTS`), so every deploy fails; and the app gets
`permission denied for table <table>` through the view until the new SP holds `SELECT` on the base
table. The synced table has to be re-created by the new SP so it holds the creator's grants.

What worked (tested live):

1. The project owner grants itself both SP roles, moves ownership, then revokes both memberships:
   ```sql
   GRANT "<old-sp>" TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
   GRANT "<new-sp>" TO CURRENT_USER WITH INHERIT TRUE, SET TRUE;
   REASSIGN OWNED BY "<old-sp>" TO "<new-sp>";
   REVOKE "<old-sp>" FROM CURRENT_USER;
   REVOKE "<new-sp>" FROM CURRENT_USER;
   ```
Run steps 2 and 3 **once for each entry in `config/tables.json`** (the shipped config has two:
`members` and `providers`). A table you skip keeps the old SP's creator grants, so the new SP's
deploy still fails on that table's index changeset.

2. The new SP drops each consumer view (it owns them now): `DROP VIEW <app_schema>.<view_name>;`
   (`view_name` defaults to `<synced table>_v`, see `config/README.md`).
3. An admin deletes each old synced table and removes it from Terraform state:
   `databricks postgres delete-synced-table synced_tables/<synced_table_id>` and
   `terraform state rm 'databricks_postgres_synced_table.t["<name>"]'`.
4. Run `deploy.sh` as the new SP (`PGUSER=<new-sp application id>`, profile/tfvars for the new SP).
   Observed live with one table: `Resources: 1 added`, ONLINE in about 30 s, Liquibase
   `Previously run: 1` (the changelog history carried over), verify 5/5. With N tables, expect
   `Resources: N added`.

## Project facts

- Lakebase here is the **Autoscaling projects** model (`databricks postgres ...`), not standalone
  database instances. Terraform resource: `databricks_postgres_synced_table`.
- Find the writer role id (used in the writer role name) on the branch endpoint:
  `SELECT rolname FROM pg_roles WHERE rolname LIKE 'databricks_writer_%';`

## CI auth — the secure way (READ before wiring GitHub)

**Most secure = OIDC / Workload Identity Federation → NO secret stored anywhere.** GitHub Actions
mints a short-lived token per run; Databricks trusts it via a federation policy scoped to this repo.
Databricks officially supports this for GitHub Actions.

Prefer this over storing a long-lived client secret in GitHub. The database password is always
minted at runtime (`databricks postgres generate-database-credential <endpoint>`, ~1h TTL) from the
target branch's READ_WRITE compute endpoint — no database-instance name, never stored. Two things
to plan for:

1. **Network access:** the runner must be able to reach your workspace. If your workspace enforces
   IP access lists, use a runner inside an allowed network (self-hosted / managed) or add an
   exception for the runner's egress.
2. **Federation policy** is typically account-level and created by an account admin on a service
   principal. Set it up once, scoped to this repository.

Until CI is wired, run `scripts/deploy.sh` locally from an authenticated machine — it is the exact
same sequence.
