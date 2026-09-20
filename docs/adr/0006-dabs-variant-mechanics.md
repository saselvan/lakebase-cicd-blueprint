# ADR 0006 — DABs variant mechanics: a Python DDL renderer in a job, codegen from tables.json, the IP-ACL boundary

> **Update (fix B):** the DABs migration originally rendered from Alembic. Rendering
> `alembic upgrade head --sql` and regex-patching its `alembic_version` bookkeeping rolled the
> whole transaction back on the second apply once a second revision existed (duplicate key on
> `alembic_version`). Decision 1 below now reads: the reconcile is a stdlib-only Python renderer
> (`dabs/render_ddl.py`) with **no migration framework and no version table**. The four SQL helpers
> are shared with the Liquibase generator (single source of the idempotent SQL).

**Status:** Accepted

## Context
The DABs lead (ADR 0005) needs three mechanics settled: how migrations run, how the single
`config/tables.json` becomes bundle resources without duplication, and an honest statement of what
"all-in on Databricks" does and doesn't fix for CI networking.

## Decision
1. **Migrations run as a bundle-managed Databricks Workflow job task, rendered by a stdlib-only
   Python module (`dabs/render_ddl.py`).** Python is native to Databricks job compute (including
   serverless) — no Java/Liquibase runtime to install in the job, and no migration framework. The
   renderer emits the same idempotent set (app role, explicit grants, `CREATE INDEX IF NOT EXISTS`,
   `CREATE OR REPLACE VIEW`) with no version table, so a re-apply is a clean reconciling no-op. It is
   also runnable standalone as `python -m dabs.render_ddl | psql`. Liquibase stays with the Terraform
   path and imports the same four SQL-building helpers.
2. **A codegen script reads `config/tables.json` and emits `dabs/resources/*.yml`** (one
   `postgres_synced_tables` + `postgres_roles` per table) as a pre-deploy step. Same single-source
   story as Terraform's `for_each` (ADR 0004) — DABs YAML can't loop a JSON list into N resources, and
   a transparent generated-YAML artifact beats a hidden Python mutator for a blueprint others adapt.
3. **Flow:** GitHub Actions → `databricks bundle validate` (PR) → `deploy --target` + `run` (merge) →
   the Workflow job waits for each synced table to reach `ONLINE`, then applies grants → indexes → view.
   The job reapplies all of them after a synced-table replacement (PK/mode/non-additive change forces
   delete+recreate) — reconciliation, not one-time setup (consistent with ADR 0002).

## Consequences
- **Honest CI-networking boundary (documented):** running the migration inside the job removes the
  GitHub-runner → Lakebase connection, but GHA still needs runner → **workspace API** for
  `bundle deploy/run`. Workspace IP ACLs can block that even with OIDC configured — OIDC is credential
  exchange, not network ingress. Fixes: IT-managed/self-hosted runner with stable egress, allowlisted
  egress, or an internally-triggered Workflow if GHA workspace access must be eliminated.
- Adding a table stays a one-line `config/tables.json` edit; the codegen step keeps DABs in sync.
- **Acceptance (not yet done):** must be verified end-to-end against a live Lakebase instance — including
  the synced-table replace → reapply test — before the README claims "live-tested." (Status flips to
  done only with command evidence.)
