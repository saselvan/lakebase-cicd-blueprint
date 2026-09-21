# Contributing

This is a reference project — small, focused changes that keep it a clear teaching example are
welcome. Please keep it generic (no workspace-specific values, no secrets).

## Before you open a PR

Run the full local gate (no cloud needed) — this is what CI enforces:

```bash
# 1. Terraform + shell + config sanity
make validate                                   # terraform validate + fmt, bash syntax, tables.json JSON
shellcheck --severity=error scripts/*.sh        # shell lint (errors only)

# 2. The offline test suite (155+ tests, no database)
.venv-dev/bin/python -m pytest dabs/tests liquibase/tests scripts/tests -q

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
bash liquibase/tests/docker_multitable_proof.sh   # two shared-schema tables + a view_name override
bash dabs/tests/docker_double_apply_proof.sh       # renderer double-apply is a clean reconciling no-op
bash scripts/tests/docker_verify_step.sh           # verify step exits non-zero on a missing grant/index
```

### What each README promise is backed by

Every claim the README makes is anchored to a test or a live run. Docker-proven = asserted against a
real Postgres in the `docker-proofs` job; offline-proven = the no-database pytest suite; live-proven =
verified once against a live Lakebase branch (recorded in `docs/DESIGN-NOTES.md`).

| Promise | Backed by | Kind |
|---|---|---|
| Per-table changeset identity — tables sharing a schema never collide in one DATABASECHANGELOG | `docker_multitable_proof.sh` + `test_generate_changelogs.py::test_shared_schema_tables_get_distinct_changeset_identities` | Docker + offline |
| Double-apply is idempotent (no version-table rollback on the 2nd apply) | `docker_double_apply_proof.sh` | Docker |
| Verify step fails on a missing grant or index | `docker_verify_step.sh` + `test_verify_step.py` (`test_missing_grant_on_view_exits_nonzero`, `test_missing_index_exits_nonzero`) | Docker + offline |
| App role reads the consumer VIEW but not the base synced table (least privilege) | least-privilege assertions in `docker_multitable_proof.sh` + `docker_double_apply_proof.sh`; live-confirmed | Docker + live |
| `view_name` override is honored — the overridden view is created, the derived `<tbl>_v` is not | `docker_multitable_proof.sh` + `test_generate_changelogs.py::test_view_name_override_appears_in_changelog`; deploy verify: `scripts/tests/test_deploy_view_name.py` | Docker + offline |
| Deploy aborts on committed-changelog drift (uses reviewed artifacts, never regenerates) | `test_deploy_drift_gate.py` | offline |
| Endpoint-based credential minting (projects API, no instance name; READ_WRITE endpoint chosen) | `test_endpoint_credential.py` | offline |
| Duplicate resolved view names are rejected at generation time | `test_generate_changelogs.py::test_duplicate_resolved_view_names_are_rejected` + `test_render_ddl.py` | offline |

Or run the minimal subset individually:

```bash
cd terraform && terraform init -backend=false && terraform validate && terraform fmt -check
bash -n scripts/*.sh
python3 -c "import json; json.load(open('config/tables.json'))"
```

## Ground rules

- **No secrets, ever.** No real hostnames, tokens, `*.tfvars`, `*.tfstate`, or credentials. Only
  `.example` templates are committed. `.gitignore` enforces this; don't bypass it. A **gitleaks**
  secret scan runs in CI on every PR — run it locally too with `pip install pre-commit && pre-commit install`.
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
| Migration SQL shape (role/grants/indexes/view) — both paths | `dabs/render_ddl.py` (the shared SQL helpers both engines use) |
| Non-standard index shape for a table | the `index_columns` for that row in `config/tables.json`, and the shared SQL helpers in `dabs/render_ddl.py` (never the generated `liquibase/generated/*.changelog.sql` — those are regenerated and drift-checked in CI) |
| Pipeline steps / ordering | `scripts/deploy.sh` |
| CI behavior | `.github/workflows/` |
