#!/usr/bin/env bash
# One-time DEMO seeding: create the source Delta tables the synced tables read FROM —
# one per entry in config/tables.json. In real use your data pipeline produces these;
# this exists only so the demo runs end to end.
#
# Each source gets: id BIGINT + one STRING column per index_column, Change Data Feed on,
# and 3 demo rows. The synced-table target schema is also created.
set -euo pipefail
PROFILE="${PROFILE:-DEFAULT}"
WH="${WAREHOUSE_ID:?set WAREHOUSE_ID to a SQL warehouse id}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="${CONFIG:-$ROOT/config/tables.json}"

run() {
  databricks api post /api/2.0/sql/statements -p "$PROFILE" \
    --json "{\"warehouse_id\":\"$WH\",\"wait_timeout\":\"30s\",\"statement\":\"$1\"}" \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('status',{}).get('state'),d.get('status',{}).get('error',{}).get('message',''))"
}

# Generate the SQL (one statement per line) from the config, then run each.
while IFS= read -r stmt; do
  [ -z "$stmt" ] && continue
  echo "-> $stmt"
  run "$stmt"
done < <(python3 - "$CONFIG" <<'PY'
import json, sys
tables = json.load(open(sys.argv[1]))
stmts, schemas = [], set()
for t in tables:
    src, tgt = t["source_table_full_name"], t["synced_table_id"]
    schemas.add(".".join(src.split(".")[:2]))   # source catalog.schema
    schemas.add(".".join(tgt.split(".")[:2]))   # synced-table target catalog.schema
for s in sorted(schemas):
    stmts.append(f"CREATE SCHEMA IF NOT EXISTS {s}")
for t in tables:
    src = t["source_table_full_name"]
    idx = t.get("index_columns", [])
    cols = ["id BIGINT NOT NULL"] + [f"{c} STRING" for c in idx]
    stmts.append(
        f"CREATE OR REPLACE TABLE {src} ({', '.join(cols)}) "
        f"TBLPROPERTIES (delta.enableChangeDataFeed = true)"
    )
    prefix = t["name"][:4].upper()
    rows = []
    for i in (1, 2, 3):
        vals = [str(i)] + [f"'{prefix}-{ci}-{i:03d}'" for ci, _ in enumerate(idx)]
        rows.append("(" + ",".join(vals) + ")")
    if rows:
        stmts.append(f"INSERT INTO {src} VALUES {', '.join(rows)}")
    stmts.append(f"SELECT count(*) FROM {src}")
print("\n".join(stmts))
PY
)

echo "seeded all demo sources from $CONFIG"
