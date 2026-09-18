# ADR 0005 — DABs is the documented lead; Terraform + Liquibase is the portable alternative

**Status:** Accepted

## Context
The original blueprint led with Terraform + Liquibase + GitHub Actions (a tool-agnostic stack that
matched the first adopter). Databricks Asset Bundles (DABs) now support Lakebase resources natively
(`postgres_synced_tables`, `postgres_roles`, `postgres_databases`, dev/prod `targets`), so a fully
"all-in on Databricks" path is buildable — one platform, fewer external tools, Git-folder friendly.

## Decision
Add a **DABs-native variant in `dabs/`** and make it the **documented lead** — the recommended path a
Databricks-first team should start from. Terraform + Liquibase is retained as a co-equal directory and
re-framed as the **portable / tool-agnostic alternative** for teams already standardized on that stack
or spanning multiple clouds. All three paths (`dabs/`, `terraform/`, `alembic/`) stay co-equal on disk;
the README/DESIGN-NOTES ordering is what changes.

## Consequences
- README now opens with the DABs quickstart; Terraform becomes "prefer this if you're multi-cloud or
  already on Terraform."
- The DABs path pairs with Alembic for migrations (ADR 0006); the Terraform path keeps Liquibase.
- Lakebase DAB support is **Beta** — pin **Databricks CLI ≥ 1.5.0** (earlier bundle-plan bug dropped
  `role_id` and recreated roles). Called out in the DABs README section.
- One state owner per object: never manage the same Lakebase resource from both Terraform and DABs.
