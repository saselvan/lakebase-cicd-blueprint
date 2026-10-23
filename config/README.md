# config/tables.json — the single source of truth

`tables.json` is **the only file most users edit to add or change tables.** Both the
Terraform layer (`terraform/main.tf`, via `for_each`) and the deploy loop
(`scripts/deploy.sh`) read it, so one edit here drives provisioning *and* migration.

It is a JSON array; add one object per synced table you want the pipeline to manage.

| Field | Meaning |
|---|---|
| `name` | Short unique key for the table (used as the Terraform `for_each` key, the Liquibase changelog filename `<name>.changelog.sql`, and the DABs resource key; validated as a unique, case-insensitive safe identifier across both generators). |
| `synced_table_id` | 3-part UC name of the synced table to create (`catalog.schema.table`). |
| `source_table_full_name` | 3-part UC Delta table to sync FROM. |
| `primary_key_columns` | Array of PK column names for the synced table. |
| `app_schema` | Postgres/UC schema the table lands in. Baked directly into the table's generated changelog and the DABs renderer output — not a Liquibase property substitution. |
| `app_role` | Read-only app role granted access (schema `USAGE` + `SELECT` on the consumer view). Baked directly into the generated DDL — not a Liquibase property substitution. |
| `index_columns` | Columns to index after load. BOTH paths carry all of them, 0/1/N — no cap. The Liquibase path generates one `003-index-<col>` changeset per column (`liquibase/generate_changelogs.py`); the DABs renderer emits one `index_statements` line per column (`dabs/render_ddl.py`). Both call the same helper. |
| `scheduling_policy` | **Optional.** How the synced table's pipeline refreshes: `SNAPSHOT` (default), `TRIGGERED`, or `CONTINUOUS`. Validated against that enum at generation time — anything else fails loudly. Emitted onto the `postgres_synced_tables` resource. Docs recommend enabling Change Data Feed on the Delta source (`ALTER TABLE … SET TBLPROPERTIES (delta.enableChangeDataFeed = true)`) for `TRIGGERED` and `CONTINUOUS` modes ([sync-tables docs](https://docs.databricks.com/aws/en/oltp/projects/sync-tables), checked 2026-10-22; Lakebase also documents Automatic change data feed); `SNAPSHOT` does not use CDF. |
| `view_name` | **Optional.** Name of the consumer view over the synced table. Defaults to `<synced_table>_v` (the last dotted part of `synced_table_id`, suffixed `_v`). Set a STABLE name to support blue/green cutover: a `CREATE OR REPLACE VIEW` can re-point the same view name onto a NEW base synced table without changing the app's configuration. During a blue/green swap the OLD entry must be given a DIFFERENT `view_name` (or be removed) BEFORE the new entry takes over the stable name — otherwise both entries resolve to the same consumer view name, and the generator now **fails loudly** on that collision. Nothing in either path drops the old view: retiring the old synced table and its view is a manual step. An override must be a safe unquoted identifier (see the identifier rule below) and is threaded through BOTH the DABs renderer and the Liquibase changelog. |

**Indexes are the one inherently table-specific spot.** Each column in `index_columns` becomes its
own `CREATE INDEX IF NOT EXISTS` changeset (0, 1, or N — no two-column cap). A table that needs a
different index shape — a composite or partial index, or a different index type — extends the
shared SQL helpers in `dabs/render_ddl.py` (or `liquibase/generate_changelogs.py`) and regenerates
`liquibase/generated/*.changelog.sql` and `dabs/resources/*.yml`; never hand-edit the generated
files (`deploy.sh` and CI run `--check` and abort on any hand-edit drift).

**Identifier rule.** Every identifier baked into the emitted DDL or filenames — `name` (also
checked for case-insensitive uniqueness across rows), `app_role`, `app_schema`, the
Postgres table name (the last part of `synced_table_id`), `view_name` (when overridden; resolved
`(app_schema, view_name)` pairs are also checked for uniqueness), and each `index_columns` entry — must be
a safe unquoted Postgres identifier: **lowercase snake_case** matching `^[a-z_][a-z0-9_]*$`,
**at most 63 characters**, and **not a reserved SQL word** (e.g. `user`, `table`, `order`). Both
the DABs renderer and the Liquibase generator validate this at generation time (one shared seam,
`validate_identifier` in `dabs/render_ddl.py`) and fail loud with a clear message rather than
emitting broken or injectable SQL — so a value like `Plan-Code` is rejected, not silently quoted.

> JSON does not support comments, which is why this note lives here rather than inline.
