# Troubleshooting & FAQ

Common issues with this pipeline, and the fix. Most trace back to one of two facts: `terraform apply`
returns *before* the table is loaded, and a sync-mode change is a full rebuild.

## Symptom → cause → fix

| Symptom | Cause | Fix |
|---|---|---|
| Indexes fail or build on an empty/partial table right after `terraform apply` | `apply` does **not** block until the initial load is `ONLINE` — it returns once the synced table is created | Run `scripts/wait_for_sync.sh <synced_table_id>` before Liquibase. `deploy.sh` already does this per table. |
| `GRANT ... ON TABLE <synced_table>` denied, even as admin | Synced tables are owned by the internal `databricks_writer_<dbid>` role; you can't grant on what you don't own | Use the **consumer view** (the generated `004-app-view` changeset): the deploy identity owns the view and grants on it. Keep the table creator and view owner the same identity. |
| `ALTER DEFAULT PRIVILEGES FOR ROLE databricks_writer_<dbid>` denied | By design — you can't set defaults on the managed writer role | Don't. Use explicit grants + the consumer view. |
| After changing a table's **sync mode**, the app lost SELECT and its indexes | A sync-mode change forces a **full replace** (destroy+recreate) → drops custom indexes and grants to pkey-only | The `runAlways:true` grant/index/view changesets reapply and restore access **only if the replace succeeds**. On a table that already has a consumer view (this reference always ships one), an in-place sync-mode change was observed to **wedge** the table (`SYNCED_TABLE_OFFLINE_FAILED`) with no redeploy recovery. Use a **blue/green swap** for a sync-mode change — see the "Changing a table's sync mode" row below. Do **not** drop the consumer view as a workaround; it destroys grants and still risks the wedge. |
| Re-running Liquibase after a replace says "nothing to execute" and access is NOT restored | The changesets are `runOnChange` (checksum unchanged → skipped) | They must be **`runAlways:true`** (as shipped). If you edited them to `runOnChange`, switch back. |
| `deploy.sh` verify step shows no rows / privilege `f` | The synced table hasn't reached `ONLINE` yet, or the app role/view wasn't created | Confirm `wait_for_sync.sh` returned `ONLINE`; check the Liquibase run applied 001–004. |
| Terraform wants to **replace** a synced table on a normal plan | You changed a `spec` field that forces replacement (e.g. `scheduling_policy`) | Expected. Pick the sync mode at create time; a mode change is a rebuild, not an in-place edit. |
| Changing a table's sync mode (SNAPSHOT/TRIGGERED/CONTINUOUS) | Sync mode is a create-time property, so the change forces a full replace of the synced table. On a table with a dependent **consumer view**, an in-place replace was observed to **wedge** the table (`SYNCED_TABLE_OFFLINE_FAILED`); a redeploy cannot clear it, and in the repro recovery required deleting/recreating the branch (open a support case on a real project) | Use a **blue/green swap** — stand up a new table with the target mode, cut over, then drop the old one (see DESIGN-NOTES → "Changing sync mode: prefer a blue/green swap"). This is the only recommended path for a sync-mode change; dropping the consumer view first is **not** a safe workaround (it destroys grants, opens an availability gap, and still risks the wedge). |
| `databricks postgres generate-database-credential <endpoint>` fails in CI | Missing/invalid auth on the runner | Use OIDC / Workload Identity Federation (see `RUNBOOK.md`), not a stored secret. |

## Known limitations of this reference

- **`scripts/seed_source.sh` seeds a single demo source table** — it does not read `config/tables.json`.
  It exists only to make the demo runnable. In real use, your source Delta tables come from your own
  pipeline, so you seed/produce them yourself (one per entry in `config/tables.json`).
- **All tables are assumed to live on one Lakebase project / branch.** The shared Terraform vars
  and the single `PROJECT`/`BRANCH` in `deploy.sh` reflect that — `deploy.sh` resolves that branch's
  READ_WRITE compute endpoint (host + credential) once and reuses it for every table. Tables spanning
  multiple projects or branches would need per-table endpoint resolution — not modeled here.
- **Index columns are carried in full (0/1/N — no cap).** `liquibase/generate_changelogs.py` emits
  one `003-index-<col>` changeset per `index_columns` entry into each table's generated changelog.
  A table needing composite/partial indexes or a different index type edits its generated changelog
  (`liquibase/generated/<name>.changelog.sql`) or extends the generator — the one table-specific spot.

## Quick checks

```bash
make validate        # terraform validate/fmt, bash syntax, tables.json JSON
databricks postgres get-synced-table synced_tables/<catalog.schema.table> -p <profile> -o json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['status']['detailed_state'])"   # ONLINE?
```
