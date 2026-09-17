#!/usr/bin/env bash
# Poll a Lakebase (Autoscaling projects) synced table until its initial snapshot is ONLINE,
# then exit 0. This gates index creation until after the load.
# Usage: wait_for_sync.sh <catalog.schema.table> [profile] [timeout_sec]
set -euo pipefail

TABLE="${1:?3-part synced table id required (catalog.schema.table)}"
PROFILE="${2:-DEFAULT}"
TIMEOUT="${3:-1800}"
interval=10
elapsed=0

while (( elapsed < TIMEOUT )); do
  state=$(databricks postgres get-synced-table "synced_tables/$TABLE" -p "$PROFILE" -o json 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',{}).get('detailed_state','?'))")
  echo "synced_table=$TABLE state=$state (${elapsed}s)"
  case "$state" in
    *ONLINE*)         echo "sync online"; exit 0 ;;
    *FAILED*|*ERROR*) echo "sync FAILED"; exit 1 ;;
  esac
  sleep "$interval"
  elapsed=$(( elapsed + interval ))
done

echo "timeout after ${TIMEOUT}s waiting for sync"; exit 1
