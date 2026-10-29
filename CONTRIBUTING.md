# Contributing

This is a reference project — small, focused changes that keep it a clear teaching example are
welcome. Please keep it generic (no workspace-specific values, no secrets).

## Before you open a PR

Run the full local gate (no cloud needed). `.github/workflows/ci.yml` defines these checks (and
runs on PRs and pushes where GitHub Actions is enabled; on the `databricks-solutions` org repo,
GitHub Actions, secret scanning, push protection, and branch protection are disabled by default
until an org admin enables them, so run the local gate and Docker proofs yourself before opening a PR):

```bash
# 1. Terraform + shell + config sanity
make validate                                   # terraform validate + fmt, bash syntax, tables.json JSON
shellcheck --severity=error scripts/*.sh        # shell lint (errors only)

# 2. The offline test suite (no database), run the way CI runs it: plain `pytest`, not
#    `python -m pytest`, which adds the repo root to sys.path and hides bad imports
.venv-dev/bin/pytest dabs/tests -q
.venv-dev/bin/pytest scripts/tests liquibase/tests -q

# 3. Both single-source-of-truth drift checks (committed artifacts == fresh generation)
python3 -m dabs.generate_resources --check      # dabs/resources vs config/tables.json
python3 liquibase/generate_changelogs.py --check # liquibase/generated vs config/tables.json

# 4. Offline bundle validate (dev + prod, --strict; no workspace)
bash dabs/scripts/offline_validate.sh
```

If you edited a generator, regenerate and commit its artifacts so the drift checks stay clean.

### Docker proofs (state-level, run against a throwaway Postgres)

CI runs these in the `docker-proofs` job; the pytest suite stays Docker-free on purpose. Run them
locally too if you touched migration SQL, the deploy verify step, or the changelog generator
(requires `docker`, plus `liquibase`/`psql` for the first and third):

```bash
bash liquibase/tests/docker_multitable_proof.sh         # two shared-schema tables + a view_name override
bash liquibase/tests/docker_view_edit_proof.sh          # runAlways view edit reruns on Liquibase 4.33
bash liquibase/tests/docker_view_column_change_proof.sh # view survives column drop/rename/reorder atomically
bash dabs/tests/docker_double_apply_proof.sh            # renderer double-apply is a clean reconciling no-op
bash scripts/tests/docker_verify_step.sh                # verify step exits non-zero on a missing grant/index
```

### What each README promise is backed by

The core pipeline guarantees below are anchored to an automated test or a dated live Lakebase run.
Docker-proven = asserted against a real Postgres in the `docker-proofs` job; offline-proven = the
no-database pytest suite; live-proven = verified against a live Lakebase branch (recorded in
`docs/DESIGN-NOTES.md`).

| Promise | Backed by | Kind |
|---|---|---|
| Per-table changeset identity — tables sharing a schema never collide in one DATABASECHANGELOG | `docker_multitable_proof.sh` + `test_generate_changelogs.py::test_shared_schema_tables_get_distinct_changeset_identities` | Docker + offline |
| Double-apply is idempotent (no version-table rollback on the 2nd apply) | `docker_double_apply_proof.sh` | Docker |
| Consumer view survives base-table column drops, renames, and reorders atomically in one transaction (while still blocking an unplanned table replace) | `docker_view_column_change_proof.sh` + `test_render_ddl.py::test_view_statements_drops_before_create_and_regrants` + `test_migration_job.py::test_apply_sql_to_lakebase_runs_in_single_transaction_not_autocommit` | Docker + offline |
| Verify step fails on a missing grant or index (in both `deploy.sh` and `pr-validate.yml`) | `docker_verify_step.sh` + `test_verify_step.py` + `test_pr_validate_loop.py` (`test_pr_validate_fails_when_verify_fails_on_first_table_and_second_succeeds`, `test_pr_validate_and_deploy_invoke_verify_with_identical_args_and_pg_env`) | Docker + offline |
| App role reads the consumer VIEW but not the base synced table (least privilege) | least-privilege assertions in `docker_multitable_proof.sh` + `docker_double_apply_proof.sh` + `docker_verify_step.sh` + `test_checks.py` + `test_generate_changelogs.py::test_only_view_select_is_granted_across_changelog`; live-confirmed | Docker + offline + live |
| `view_name` override is honored — the overridden view is created, the derived `<tbl>_v` is not | `docker_multitable_proof.sh` + `test_generate_changelogs.py::test_view_name_override_appears_in_changelog`; deploy verify: `scripts/tests/test_deploy_view_name.py` | Docker + offline |
| Deploy aborts on committed-changelog drift (uses reviewed artifacts, never regenerates) | `test_deploy_drift_gate.py` | offline |
| The Terraform-path deploy checks the target and the changelogs before `terraform apply` (a wrong target, a conflicting `terraform.tfvars`, a bad database name, or drift stops it before anything changes) | `test_deploy_target_parity.py::test_attacker_6_*`; mutation-checked | offline |
| Renamed or deleted entries leave no stale generated file: write mode removes only files the generator wrote (never hand-written files, symlinks, or files that merely quote the marker), prints each removal, and `--check` removes nothing | `test_generate_resources.py` and `test_generate_changelogs.py` (the `renamed_entries`, `hand_written`, `symlink`, `only_quotes_the_marker` and `check_mode_deletes` tests); mutation-checked | offline |
| `docs/CHANGE-IMPACT.md` names only tests and scripts that exist | `test_docs_parity.py::test_change_impact_cites_only_tests_and_scripts_that_exist` | offline |
| Deploy, Terraform, Liquibase, and verify target one project, branch, profile, and database (and `make branch` threads `PROFILE`) | `test_deploy_target_parity.py` + `test_deploy_workflow.py` + `test_wait_for_sync.py` | offline |
| Endpoint-based credential minting (projects API, no instance name; READ_WRITE endpoint with ready host and resource name required on both paths) | `test_endpoint_credential.py::test_shell_and_dabs_endpoint_parity_matrix` + `test_migration_job.py::test_select_endpoint_and_conninfo_attacker_matrix` | offline |
| Duplicate table names and duplicate resolved view names are rejected at generation time across both generators | `test_generate_changelogs.py` (`test_duplicate_table_names_rejected_in_write_changelogs_and_load_tables`, `test_duplicate_resolved_view_names_are_rejected`) + `test_render_ddl.py` + `test_generate_resources.py` | offline |

Or run the minimal subset individually:

```bash
cd terraform && terraform init -backend=false && terraform validate && terraform fmt -check
bash -n scripts/*.sh
python3 -c "import json; json.load(open('config/tables.json'))"
```

## Ground rules

- **No secrets, ever.** No real hostnames, tokens, `*.tfvars`, `*.tfstate`, or credentials. Only
  `.example` templates are committed. `.gitignore` enforces this; don't bypass it. `.github/workflows/ci.yml`
  defines a **gitleaks** secret scan (which runs where GitHub Actions is enabled; on the
  `databricks-solutions` org repo Actions, secret scanning, and push protection are off by default)
  — always run it locally before pushing with `pip install pre-commit && pre-commit install` (and
  `pre-commit run --all-files`).
- **No workspace- or customer-specific values.** Use the placeholders (`<your-...>`, `my_catalog`,
  `cicd_proj`). Keep the demo names neutral.
- **Keep changesets idempotent.** Grants/indexes/views are `runAlways:true` and must stay safe to
  re-run (`GRANT`, `CREATE INDEX IF NOT EXISTS`, `CREATE OR REPLACE VIEW`).
- **Adding a table is data, not code** — a new entry in `config/tables.json`, not a new resource.
- **Document the "why."** If a change reflects tested Lakebase behavior, note it in
  `docs/DESIGN-NOTES.md` so the reasoning travels with the code.

## What to change where

| Change | Edit |
|---|---|
| Tables the pipeline manages | `config/tables.json` |
| Shared workspace settings | `terraform/terraform.tfvars.example` (+ your own `.tfvars`) |
| Migration SQL shape (role/grants/indexes/view) — both paths | `dabs/render_ddl.py` (the shared SQL helpers both engines use). Read `docs/CHANGE-IMPACT.md` first: it lists what each grant, role and ownership step needs, what breaks, and which test proves it |
| Non-standard index shape for a table | the `index_columns` for that row in `config/tables.json`, and the shared SQL helpers in `dabs/render_ddl.py` (never the generated `liquibase/generated/*.changelog.sql` — those are regenerated and drift-checked in CI) |
| Pipeline steps / ordering | `scripts/deploy.sh` |
| CI behavior | `.github/workflows/` |
