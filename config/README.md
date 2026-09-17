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
| `index_columns` | Up to 2 columns to index after load (Liquibase `${index_col_1}`/`${index_col_2}`). |

**Indexes are the one inherently table-specific spot.** The two-index template in
`liquibase/changelog/003-indexes.sql` covers the common case (0, 1, or 2 columns from
`index_columns`). A table that needs a different index shape — more than two indexes, a
composite/partial index, or a different index type — customizes that changeset directly.

> JSON does not support comments, which is why this note lives here rather than inline.
