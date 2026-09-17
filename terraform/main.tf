# Lakebase Autoscaling PROJECTS model. Standalone database instances are legacy — build here.
# The project + production branch already exist; Terraform manages the synced tables on them.
# SNAPSHOT mode keeps the previous copy until the new sync completes.

# Single source of truth: config/tables.json. Both this file and scripts/deploy.sh read it,
# so adding a table is a one-line edit there — no Terraform changes needed.
locals {
  tables = { for t in jsondecode(file("${path.module}/../config/tables.json")) : t.name => t }
}

# One synced table per entry in config/tables.json (for_each keyed by `name`).
resource "databricks_postgres_synced_table" "t" {
  for_each = local.tables

  synced_table_id = each.value.synced_table_id # catalog.schema.table (UC)

  spec = {
    branch                             = var.branch
    postgres_database                  = var.logical_database
    source_table_full_name             = each.value.source_table_full_name
    primary_key_columns                = each.value.primary_key_columns
    scheduling_policy                  = "SNAPSHOT"
    create_database_objects_if_missing = true

    new_pipeline_spec = {
      storage_catalog = var.storage_catalog
      storage_schema  = var.storage_schema
    }
  }
}
