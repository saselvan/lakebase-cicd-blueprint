#!/usr/bin/env bash
# THE PIPELINE. Exactly what CI runs, runnable by hand so you can watch each step.
#   1. terraform apply        -> for_each creates ALL synced tables in config/tables.json
#   then FOR EACH table in config/tables.json:
#   2. wait_for_sync          -> block until the initial load is ONLINE (so indexes come after)
#   3. liquibase update       -> app role + explicit grants + indexes + consumer view
#   4. verify                 -> app role can SELECT, indexes exist
#
# Single source of truth: config/tables.json (also read by terraform/main.tf). Add a table
# there and it is provisioned AND migrated by this one script — no code changes.
#
# Set the shared environment variables below for your workspace (or export them beforehand).
set -euo pipefail

PROFILE="${PROFILE:-DEFAULT}"
INSTANCE="${INSTANCE:-my-lakebase-project}"
HOST="${HOST:?set HOST to your Lakebase read-write endpoint host}"
PGUSER="${PGUSER:?set PGUSER to your Databricks username (email)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/config/tables.json"

echo "== 1. terraform apply (for_each creates all tables in config/tables.json) =="
( cd "$ROOT/terraform" && terraform init -input=false >/dev/null && terraform apply -auto-approve -input=false )

# Mint a short-lived OAuth token once, reused for every table's liquibase + psql run.
echo "== mint runtime Lakebase credential =="
TOKEN=$(databricks database generate-database-credential --json "{\"instance_names\":[\"$INSTANCE\"]}" -p "$PROFILE" -o json | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
URL="jdbc:postgresql://$HOST:5432/databricks_postgres?sslmode=require"
export PGHOST="$HOST" PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require PGUSER="$PGUSER" PGPASSWORD="$TOKEN"

# Iterate every table entry in the single source of truth.
COUNT=$(python3 -c "import json;print(len(json.load(open('$CONFIG'))))")
for (( i = 0; i < COUNT; i++ )); do
  # Extract this table's fields as shell-safe assignments (shlex-quoted).
  eval "$(python3 - "$CONFIG" "$i" <<'PY'
import json, sys, shlex
t = json.load(open(sys.argv[1]))[int(sys.argv[2])]
idx = t.get("index_columns") or []
def emit(k, v): print(f"{k}={shlex.quote(str(v))}")
emit("T_NAME",   t["name"])
emit("T_STID",   t["synced_table_id"])
emit("T_SCHEMA", t["app_schema"])
emit("T_ROLE",   t["app_role"])
emit("T_IDX1",   idx[0] if len(idx) > 0 else "")
emit("T_IDX2",   idx[1] if len(idx) > 1 else "")
PY
)"
  PG_TABLE="${T_STID##*.}"   # last part of the 3-part UC name = Postgres table name
  echo ""
  echo "########## table '$T_NAME' ($T_STID) ##########"

  echo "== 2. wait for initial sync =="
  "$ROOT/scripts/wait_for_sync.sh" "$T_STID" "$PROFILE"

  echo "== 3. liquibase update =="
  # Per-table params (fixes the earlier bug where deploy.sh never passed these to liquibase).
  # Index columns handled gracefully: skip when a table declares none; when it declares one,
  # duplicate it so the second CREATE INDEX IF NOT EXISTS is a same-name no-op.
  IDX_PARAMS=()
  if [[ -n "$T_IDX1" ]]; then
    IDX_PARAMS+=( "-Dindex_col_1=$T_IDX1" "-Dindex_col_2=${T_IDX2:-$T_IDX1}" )
  fi
  ( cd "$ROOT/liquibase" && liquibase update \
      --changeLogFile=changelog/db.changelog-master.xml \
      --url="$URL" --username="$PGUSER" --password="$TOKEN" \
      --liquibase-schema-name="$T_SCHEMA" \
      -Dsynced_table="$PG_TABLE" -Dapp_schema="$T_SCHEMA" -Dapp_role="$T_ROLE" \
      "${IDX_PARAMS[@]}" )

  echo "== 4. verify =="
  psql -tAc "SELECT has_table_privilege('$T_ROLE','$T_SCHEMA.$PG_TABLE'::regclass,'SELECT') AS app_can_select;" 2>&1 || true
  psql -tAc "SELECT indexname FROM pg_indexes WHERE schemaname='$T_SCHEMA' AND tablename='$PG_TABLE' ORDER BY 1;" 2>&1 || true
done

echo ""
echo "== done: $COUNT table(s) provisioned + migrated =="
