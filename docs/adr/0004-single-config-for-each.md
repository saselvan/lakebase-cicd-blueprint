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
- The Liquibase path generates one changelog per table from this file
  (`liquibase/generate_changelogs.py`, mirroring `dabs/generate_resources.py`), with values baked
  in. A distinct changelog FILE per table gives each changeset a distinct identity, so tables that
  share one `app_schema` never collide in a shared `DATABASECHANGELOG` (property substitution into a
  single shared changelog did collide — see DESIGN-NOTES). Generated changelogs are committed and
  drift-checked (`--check`).
- **Index columns are the one inherently table-specific spot** — the generator emits one index
  changeset per `index_columns` entry (0/1/N, no cap); unusual index shapes edit the generated
  changelog.
- All tables are assumed to share one project/branch/endpoint host (see TROUBLESHOOTING).
