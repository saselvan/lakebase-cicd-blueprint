variable "databricks_profile" {
  description = "Local Databricks CLI profile to authenticate with."
  type        = string
  default     = "DEFAULT"
}

variable "project_id" {
  description = "Lakebase Autoscaling project id."
  type        = string
  default     = "my-lakebase-project"
}

variable "branch" {
  description = "Full branch resource path the synced table lives on."
  type        = string
  default     = "projects/my-lakebase-project/branches/production"
}

variable "logical_database" {
  description = "Postgres database name inside the branch."
  type        = string
  default     = "databricks_postgres"
}

variable "storage_catalog" {
  description = "UC catalog for pipeline storage + the synced-table online view. Must exist."
  type        = string
  default     = "my_catalog"
}

variable "storage_schema" {
  description = "UC schema (must exist) + Postgres schema the synced table lands in."
  type        = string
  default     = "cicd_proj"
}

# NOTE: per-table settings (synced_table_id, source_table_full_name, primary_key_columns,
# app_schema, app_role, index_columns) are NOT variables — they live in config/tables.json
# and are read by both Terraform (for_each in main.tf) and scripts/deploy.sh.
