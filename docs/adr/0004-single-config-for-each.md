# ADR 0004 — One `config/tables.json`, read by both Terraform and the deploy loop

**Status:** Accepted

## Context
Managing many synced tables shouldn't mean copy-pasting Terraform resources or editing scripts per
table. Terraform (HCL) and the deploy script (bash) need the same list of tables.

## Decision
`config/tables.json` is the **single source of truth**. Terraform does `for_each` over
`jsondecode(file(...))`; `scripts/deploy.sh` reads the same file and loops. Adding a table is a
one-line JSON edit — no new resource, no script change.

## Consequences
- One place to add/remove tables; both layers stay in sync by construction.
- Liquibase changesets are parametrized (`${synced_table}`, `${app_schema}`, `${app_role}`,
  `${index_col_1/2}`) so one changelog serves every table.
- **Index columns are the one inherently table-specific spot** — the two-column template in
  `003-indexes.sql` covers the common case; unusual index shapes edit that changeset.
- All tables are assumed to share one project/branch/instance/host (see TROUBLESHOOTING).
