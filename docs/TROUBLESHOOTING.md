# Troubleshooting & FAQ

Common issues with this pipeline, and the fix. Most trace back to one of two facts: `terraform apply`
returns *before* the table is loaded, and a sync-mode change is a full rebuild.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| Indexes fail or build on an empty/partial table right after `terraform apply` | `apply` does **not** block until the initial load is `ONLINE` — it returns once the synced table is created | Run `scripts/wait_for_sync.sh <synced_table_id>` before Liquibase. `deploy.sh` already does this per table. |
| `GRANT ... ON TABLE <synced_table>` denied, even as admin | Synced tables are owned by the internal `databricks_writer_<dbid>` role; you can't grant on what you don't own | Use the **consumer view** (`004-app-view.sql`): the deploy identity owns the view and grants on it. Keep the table creator and view owner the same identity. |
| `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` denied | By design — you can't set defaults on the managed writer role | Don't. Use explicit grants + the consumer view. |
| After changing a table's **sync mode**, the app lost SELECT and its indexes | A sync-mode change forces a **full replace** (destroy+recreate) → drops custom indexes and grants to pkey-only | Re-run `deploy.sh`. Because the grant/index/view changesets are `runAlways:true`, they reapply and restore access. Treat a mode change as a planned rebuild. |
| Re-running Liquibase after a replace says "nothing to execute" and access is NOT restored | The changesets are `runOnChange` (checksum unchanged → skipped) | They must be **`runAlways:true`** (as shipped). If you edited them to `runOnChange`, switch back. |
| `deploy.sh` verify step shows no rows / privilege `f` | The synced table hasn't reached `ONLINE` yet, or the app role/view wasn't created | Confirm `wait_for_sync.sh` returned `ONLINE`; check the Liquibase run applied 001–004. |
| Terraform wants to **replace** a synced table on a normal plan | You changed a `spec` field that forces replacement (e.g. `scheduling_policy`) | Expected. Pick the sync mode at create time; a mode change is a rebuild, not an in-place edit. |
| `databricks database generate-database-credential` fails in CI | Missing/invalid auth on the runner | Use OIDC / Workload Identity Federation (see `RUNBOOK.md`), not a stored secret. |

## Known limitations of this reference

- **`scripts/seed_source.sh` seeds a single demo source table** — it does not read `config/tables.json`.
  It exists only to make the demo runnable. In real use, your source Delta tables come from your own
  pipeline, so you seed/produce them yourself (one per entry in `config/tables.json`).
- **All tables are assumed to live on one Lakebase project / branch / instance / host.** The shared
  Terraform vars and the single `INSTANCE`/`HOST` in `deploy.sh` reflect that. Tables spanning
  multiple projects or instances would need per-table `instance`/`host` — not modeled here.
- **The `003-indexes.sql` template covers up to two index columns** (`${index_col_1}`/`${index_col_2}`).
  A table needing more indexes, composite/partial indexes, or a different index type customizes that
  changeset directly — it's the one inherently table-specific spot.

## Quick checks

```bash
make validate        # terraform validate/fmt, bash syntax, tables.json JSON
databricks postgres get-synced-table synced_tables/<catalog.schema.table> -p <profile> -o json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['status']['detailed_state'])"   # ONLINE?
```
