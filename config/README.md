# config/tables.json — the single source of truth

`tables.json` is **the only file most users edit to add or change tables.** Both the
Terraform layer (`terraform/main.tf`, via `for_each`) and the deploy loop
(`scripts/deploy.sh`) read it, so one edit here drives provisioning *and* migration.

It is a JSON array; add one object per synced table you want the pipeline to manage.

| Field | Meaning |
|---|---|
| `name` | Short unique key for the table (used as the Terraform `for_each` key and in logs). |
| `synced_table_id` | 3-part UC name of the synced table to create (`catalog.schema.table`). |
| `source_table_full_name` | 3-part UC Delta table to sync FROM. |
| `primary_key_columns` | Array of PK column names for the synced table. |
| `app_schema` | Postgres/UC schema the table lands in (Liquibase `${app_schema}`). |
| `app_role` | Read-only app role granted access (Liquibase `${app_role}`). |
| `index_columns` | Columns to index after load. BOTH paths carry all of them, 0/1/N — no cap. The Liquibase path generates one `003-index-<col>` changeset per column (`liquibase/generate_changelogs.py`); the DABs/Alembic path emits one `_index_statements` line per column. |

**Indexes are the one inherently table-specific spot.** Each column in `index_columns` becomes its
own `CREATE INDEX IF NOT EXISTS` changeset (0, 1, or N — no two-column cap). A table that needs a
different index shape — a composite or partial index, or a different index type — edits its
generated changelog (`liquibase/generated/<name>.changelog.sql`) or extends the generator.

> JSON does not support comments, which is why this note lives here rather than inline.
