# Lakebase Autoscaling PROJECTS model. Standalone database instances are legacy — build here.
# The project + production branch already exist; Terraform manages the synced table on them.
# SNAPSHOT mode keeps the previous copy until the new sync completes.
resource "databricks_postgres_synced_table" "members" {
  synced_table_id = var.synced_table_id # catalog.schema.table (UC)

  spec = {
    branch                             = var.branch
    postgres_database                  = var.logical_database
    source_table_full_name             = var.source_table_full_name
    primary_key_columns                = ["id"]
    scheduling_policy                  = "SNAPSHOT"
    create_database_objects_if_missing = true

    new_pipeline_spec = {
      storage_catalog = var.storage_catalog
      storage_schema  = var.storage_schema
    }
  }
}
