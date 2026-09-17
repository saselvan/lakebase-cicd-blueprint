# ADR 0003 — Wait-for-ONLINE gate before migrating

**Status:** Accepted

## Context
`terraform apply` of `databricks_postgres_synced_table` does **not** block until the table's initial
load is `ONLINE` — verified live, the apply returned in ~12s while the table was still
`PROVISIONING`, reaching `ONLINE` later. If Liquibase runs immediately after `apply`, indexes try to
build on an empty or partial table.

## Decision
The pipeline **polls the synced-table status until it reports `ONLINE`** (`scripts/wait_for_sync.sh`)
before running Liquibase. `deploy.sh` does this per table.

## Consequences
- Indexes and grants always apply against a loaded table.
- The pipeline is a poll loop, not a single linear apply — a deliberate step, not incidental.
- Provider "wait" behavior is not something to rely on; the explicit gate is the contract.
