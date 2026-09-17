terraform {
  required_version = ">= 1.5"
  required_providers {
    databricks = {
      source = "databricks/databricks"
      # Lakebase postgres_* resources need a recent provider.
      # Confirm the exact minimum version on the machine that runs CI.
      version = ">= 1.90.0"
    }
  }
}

provider "databricks" {
  profile = var.databricks_profile
}
