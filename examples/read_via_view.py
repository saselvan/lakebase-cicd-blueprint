#!/usr/bin/env python3
"""
Illustrative consumer: read a synced table THROUGH its consumer view (ADR 0001).

An app reads the view (e.g. `cicd_proj.members_v`) that the deploy identity created and granted
to the app role — never the writer-owned base table, and never as a superuser.

This is a minimal example. For production, use a connection POOL and let the OAuth token refresh
per connection (tokens are short-lived, ~1h). See the Databricks tutorial:
https://docs.databricks.com/aws/en/oltp/projects/tutorial-databricks-apps-autoscaling

Requirements: psycopg[binary] (psycopg3), Databricks CLI authenticated.
Env: PGHOST (Lakebase read-write endpoint host), PGUSER (your Databricks username),
     PROFILE (CLI profile), INSTANCE (Lakebase project/instance name), VIEW (schema.view).
"""
import json
import os
import subprocess

import psycopg  # psycopg3


def mint_token(instance: str, profile: str) -> str:
    """Short-lived Postgres OAuth token — minted at runtime, never stored."""
    out = subprocess.run(
        ["databricks", "database", "generate-database-credential",
         "--json", json.dumps({"instance_names": [instance]}), "-p", profile, "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)["token"]


def main() -> None:
    host = os.environ["PGHOST"]
    user = os.environ["PGUSER"]
    view = os.environ.get("VIEW", "cicd_proj.members_v")
    token = mint_token(os.environ["INSTANCE"], os.environ.get("PROFILE", "DEFAULT"))

    # In production use psycopg_pool.ConnectionPool with a per-connection token callback.
    with psycopg.connect(
        host=host, port=5432, dbname="databricks_postgres",
        user=user, password=token, sslmode="require",
    ) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {view}")
        print(f"{view}: {cur.fetchone()[0]} rows readable via the view")


if __name__ == "__main__":
    main()
