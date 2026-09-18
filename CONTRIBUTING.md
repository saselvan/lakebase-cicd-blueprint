# Contributing

This is a reference project — small, focused changes that keep it a clear teaching example are
welcome. Please keep it generic (no workspace-specific values, no secrets).

## Before you open a PR

Run the local checks (no cloud needed):

```bash
make validate        # terraform validate + fmt check, bash syntax, tables.json JSON check
```

Or individually:

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
| Non-standard index shape for a table | `liquibase/changelog/003-indexes.sql` |
| Pipeline steps / ordering | `scripts/deploy.sh` |
| CI behavior | `.github/workflows/` |
