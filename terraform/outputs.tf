output "synced_table_id" {
  value = databricks_postgres_synced_table.members.synced_table_id
}

output "synced_table_name" {
  description = "Full resource name of the synced table."
  value       = databricks_postgres_synced_table.members.name
}

output "synced_table_status" {
  description = "Computed status block (data-sync state, pipeline id when present)."
  value       = databricks_postgres_synced_table.members.status
}
