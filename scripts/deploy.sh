#!/usr/bin/env bash
# THE PIPELINE. Exactly what CI runs, runnable by hand so you can watch each step.
#   1. terraform apply  -> creates the synced table (Delta -> Lakebase Postgres)
#   2. wait_for_sync    -> block until the initial load is ONLINE (so indexes come after)
#   3. liquibase update -> app role + explicit grants + indexes + consumer view
#   4. verify           -> app role can SELECT, indexes exist
#
# Set the environment variables below for your workspace (or export them beforehand).
set -euo pipefail

PROFILE="${PROFILE:-DEFAULT}"
INSTANCE="${INSTANCE:-my-lakebase-project}"
SYNCED_TABLE="${SYNCED_TABLE:-my_catalog.cicd_proj.members}"
APP_SCHEMA="${APP_SCHEMA:-cicd_proj}"
APP_ROLE="${APP_ROLE:-members_app_ro}"
HOST="${HOST:?set HOST to your Lakebase read-write endpoint host}"
PGUSER="${PGUSER:?set PGUSER to your Databricks username (email)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "== 1. terraform apply =="
( cd "$ROOT/terraform" && terraform init -input=false >/dev/null && terraform apply -auto-approve -input=false )

echo "== 2. wait for initial sync =="
"$ROOT/scripts/wait_for_sync.sh" "$SYNCED_TABLE" "$PROFILE"

echo "== 3. liquibase update =="
TOKEN=$(databricks database generate-database-credential --json "{\"instance_names\":[\"$INSTANCE\"]}" -p "$PROFILE" -o json | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
URL="jdbc:postgresql://$HOST:5432/databricks_postgres?sslmode=require"
( cd "$ROOT/liquibase" && liquibase update \
    --changeLogFile=changelog/db.changelog-master.xml \
    --url="$URL" --username="$PGUSER" --password="$TOKEN" \
    --liquibase-schema-name="$APP_SCHEMA" )

echo "== 4. verify =="
export PGHOST="$HOST" PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require PGUSER="$PGUSER" PGPASSWORD="$TOKEN"
PG_TABLE="${SYNCED_TABLE##*.}"   # last part of the 3-part UC name = Postgres table name
psql -tAc "SELECT has_table_privilege('$APP_ROLE','$APP_SCHEMA.$PG_TABLE'::regclass,'SELECT') AS app_can_select;" 2>&1 || true
psql -tAc "SELECT indexname FROM pg_indexes WHERE schemaname='$APP_SCHEMA' AND tablename='$PG_TABLE' ORDER BY 1;" 2>&1 || true
echo "== done =="
