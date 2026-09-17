#!/usr/bin/env bash
# One-time: create the demo source Delta table the synced table reads FROM.
# In real life this is produced by your data pipeline, not the CI/CD.
set -euo pipefail
PROFILE="${PROFILE:-DEFAULT}"
WH="${WAREHOUSE_ID:?set WAREHOUSE_ID to a SQL warehouse id}"
SRC="${SRC:-my_catalog.cicd_app.members_src}"
SRC_SCHEMA="${SRC%.*}"                                   # catalog.schema of the source
TARGET_SCHEMA="${TARGET_SCHEMA:-my_catalog.cicd_proj}"   # UC schema the synced table lands in

run() { databricks api post /api/2.0/sql/statements -p "$PROFILE" \
  --json "{\"warehouse_id\":\"$WH\",\"wait_timeout\":\"30s\",\"statement\":\"$1\"}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('state'),d.get('status',{}).get('error',{}).get('message',''))"; }

run "CREATE SCHEMA IF NOT EXISTS ${SRC_SCHEMA}"
run "CREATE SCHEMA IF NOT EXISTS ${TARGET_SCHEMA}"       # synced-table target must exist in UC
run "CREATE OR REPLACE TABLE ${SRC} (id BIGINT NOT NULL, member_id STRING, plan_code STRING) TBLPROPERTIES (delta.enableChangeDataFeed = true)"
run "INSERT INTO ${SRC} VALUES (1,'M001','GOLD'),(2,'M002','SILVER'),(3,'M003','BRONZE')"
run "SELECT count(*) FROM ${SRC}"
echo "seeded $SRC and target schema $TARGET_SCHEMA"
