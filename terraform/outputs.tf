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

output "logical_database" {
  description = "Postgres database name inside the branch (from var.logical_database)."
  value       = var.logical_database
}

output "project_id" {
  description = "Lakebase Autoscaling project id (from var.project_id)."
  value       = var.project_id
}

output "branch" {
  description = "Full branch resource path the synced table lives on (from var.branch)."
  value       = var.branch
}

output "databricks_profile" {
  description = "Databricks CLI profile used by the provider (from var.databricks_profile)."
  value       = var.databricks_profile
}


