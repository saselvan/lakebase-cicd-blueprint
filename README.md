# Lakebase CI/CD Reference

A working reference for a **Lakebase CI/CD pipeline**: Terraform provisions the synced table,
GitHub Actions orchestrates, and Liquibase applies database roles, grants, indexes, and a consumer
view — all idempotent and self-healing. Built on the Lakebase **Autoscaling projects** model
(`databricks_postgres_*`), which is the model to build on.

> This is sample code, not a turnkey deployment. Point the variables at your own workspace, review
> each step, and run it.

---

## Architecture

**Pipeline** — the deploy sequence, end to end:

![CI/CD pipeline: git push → terraform apply → wait for sync → liquibase update → verify](docs/pipeline.customer.light.png)

*Terraform creates and loads the synced table, the pipeline waits for the initial load to reach
ONLINE, then Liquibase applies the role, grants, indexes, and view — and finally verifies access.*

**Access model** — why default privileges don't work, and the supported path:

![Access model: writer-owned synced table, explicit grants, and a consumer view owned by the deploy identity](docs/access-model.customer.light.png)

*Synced tables are owned by a managed writer role whose default privileges you cannot change. The
deploy identity instead applies explicit grants and owns a consumer view that it can freely grant —
so app access never depends on a privilege no user can hold.*

---

## The pattern

The pipeline is four ordered steps, run identically by hand (`scripts/deploy.sh`) and by CI:

1. **Terraform provisions** the synced table (`databricks_postgres_synced_table`) — Delta → Lakebase
   Postgres. Terraform owns its state, so reruns are a no-op.
2. **Wait for ONLINE** (`scripts/wait_for_sync.sh`) — poll until the initial snapshot has loaded.
   This is what guarantees indexes are built *after* the data lands.
3. **Liquibase applies** the database objects, in order:
   - `001-app-role.sql` — an idempotent read-only app role.
   - `002-app-grants.sql` — **explicit** `GRANT USAGE`/`GRANT SELECT` on the writer-owned synced table.
   - `003-indexes.sql` — `CREATE INDEX IF NOT EXISTS`, after the load.
   - `004-app-view.sql` — a consumer **view** owned by the deploy identity, with the grant on the view.
4. **Verify** — the app role can `SELECT`, and the indexes exist.

### The view + no-superuser access model

Synced tables are created and owned by a managed writer role (`databricks_writer_<dbid>`). You
cannot join that role or change its default privileges — not even as a superuser. So the usual
`ALTER DEFAULT PRIVILEGES` approach is a dead end.

The supported path uses two facts:

- The identity that **creates** the synced table automatically owns it and gets `SELECT`.
- That same identity runs Liquibase, so it can `CREATE OR REPLACE VIEW` over the synced table and,
  because it **owns the view**, grant consumers `SELECT` on the view.

Consumers read the view, not the base table. Access is granted on an object the deploy identity
owns, so it never depends on a privilege no user can hold — and no `databricks_superuser` is needed.
The grant/view/index changesets are `runAlways:true`, so they are reapplied on every deploy and
recreated after a table replace.

---

## Key findings (from live testing)

Verified end to end on the Autoscaling projects model (PostgreSQL 16, Databricks Terraform provider
v1.132.0, Terraform v1.16.2, Liquibase 4.33.0):

- **`terraform apply` does NOT block until the sync is ONLINE.** The resource returns once the
  synced table is created; the initial load runs asynchronously. Use the `wait_for_sync.sh` gate
  before building indexes, or they'll try to build on an empty/partial table.
- **`CREATE INDEX IF NOT EXISTS` is idempotent** — safe to run on every deploy.
- **All sync modes persist indexes and grants across a same-mode refresh.** A routine re-sync keeps
  the previous copy until the new load finishes; indexes and grants survive.
- **A sync-mode CHANGE forces a full replace that drops indexes and grants** (the Postgres table is
  recreated to pkey-only). This is why the grant, index, and view changesets are `runAlways:true` —
  they restore themselves on the next deploy. **Treat a mode change as a planned rebuild**, not a
  routine operation.
- **`ALTER DEFAULT PRIVILEGES` on the writer role is denied by design** — the explicit-grants +
  consumer-view pattern is the supported alternative.
- **Copy-on-write branches isolate risk:** a destructive migration (e.g. dropping an index) on an
  ephemeral branch leaves the production copy untouched.

---

## Repo layout

```
terraform/          databricks_postgres_synced_table on the project
liquibase/          changelog: 001 app role, 002 explicit grants, 003 indexes, 004 consumer view
scripts/            seed_source.sh, wait_for_sync.sh, deploy.sh, branch_test.sh
.github/workflows/  deploy.yml, pr-validate.yml, pr-cleanup.yml (reference-only; see RUNBOOK CI auth)
docs/               ANSWERS.md (Q&A), pipeline + access-model diagrams
```

---

## Prerequisites

- A Databricks workspace with a Lakebase **Autoscaling project** and a `production` branch.
- Databricks CLI, authenticated to your workspace (a named CLI profile).
- Terraform ≥ 1.5 and the `databricks/databricks` provider ≥ 1.90.
- Liquibase (OSS) ≥ 4.33 and `psql`.
- A SQL warehouse (for seeding the demo source table) and an existing UC catalog + schema.

---

## How to run

```bash
export PROFILE=<your-cli-profile>
export WAREHOUSE_ID=<your-sql-warehouse-id>
export HOST=<your-lakebase-rw-endpoint-host>
export PGUSER=<your-databricks-username>

# copy and edit the examples, then:
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # edit for your workspace

./scripts/seed_source.sh   # one-time: demo Delta source table + UC schemas
./scripts/deploy.sh        # THE pipeline: terraform apply -> wait sync -> liquibase update -> verify
```

`deploy.sh` is the exact sequence GitHub Actions runs. See `RUNBOOK.md` for the walkthrough and the
CI auth guidance, and `docs/ANSWERS.md` for a point-by-point Q&A.

### Branching (test risky changes safely)

```bash
./scripts/branch_test.sh <your-project-id> pr-123
# creates an ephemeral copy-on-write branch, runs your migration against it, then you delete it
```

Two distinct concerns, two tools: **load speed** → load-then-index (`CREATE INDEX CONCURRENTLY` so
apps don't block); **risky rebuild / drop / move** → test on an ephemeral branch, then promote.
Don't use one to solve the other.

---

## Security notes

- **No stored database credentials.** The Postgres password is minted at runtime with
  `databricks database generate-database-credential` (short-lived OAuth token, ~1h TTL).
- **No secrets in the repo.** `.gitignore` excludes `*.tfstate*`, `*.tfvars`, `*.env`, and
  `liquibase.properties`. Only `.example` templates are committed.
- **Terraform state is gitignored** — keep it in a secure remote backend for real use.
- **Recommended CI auth is OIDC / Workload Identity Federation** (no long-lived secret stored in
  GitHub). See `RUNBOOK.md` → "CI auth".
- The included workflows are **reference-only** and manual-trigger — wire them up deliberately once
  auth and network access are in place.
