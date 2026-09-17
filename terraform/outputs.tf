# for_each-aware: emit a map keyed by the config `name` for every managed synced table.
output "synced_tables" {
  description = "Map of provisioned synced tables (keyed by config/tables.json name)."
  value = {
    for name, t in databricks_postgres_synced_table.t : name => {
      synced_table_id = t.synced_table_id
      name            = t.name   # full resource name of the synced table
      status          = t.status # computed status block (data-sync state, pipeline id when present)
    }
  }
}
