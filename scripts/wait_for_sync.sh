#!/usr/bin/env bash
# Poll a Lakebase (Autoscaling projects) synced table until its initial snapshot is ONLINE,
# then exit 0. This gates index creation until after the load.
# Usage: wait_for_sync.sh <catalog.schema.table> [profile] [timeout_sec]
# WAIT_INTERVAL sets the poll interval in seconds (default 10).
set -euo pipefail

TABLE="${1:?3-part synced table id required (catalog.schema.table)}"
PROFILE="${2-${PROFILE-DEFAULT}}"
TIMEOUT="${3:-1800}"
interval="${WAIT_INTERVAL:-10}"
[[ "$interval" =~ ^[1-9][0-9]*$ ]] || { echo "WAIT_INTERVAL must be a whole number of seconds >= 1, got '$interval'"; exit 2; }
elapsed=0

pflag=()
[ -n "$PROFILE" ] && pflag=(-p "$PROFILE")

while (( elapsed < TIMEOUT )); do
  state=$(databricks postgres get-synced-table "synced_tables/$TABLE" ${pflag[@]+"${pflag[@]}"} -o json 2>/dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('status',{}).get('detailed_state','?'))")
  echo "synced_table=$TABLE state=$state (${elapsed}s)"
  # FAILED/ERROR first: SYNCED_TABLE_ONLINE_PIPELINE_FAILED contains ONLINE too, and must fail.
  case "$state" in
    *FAILED*|*ERROR*) echo "sync FAILED"; exit 1 ;;
    *ONLINE*)         echo "sync online"; exit 0 ;;
  esac
  sleep "$interval"
  elapsed=$(( elapsed + interval ))
done

echo "timeout after ${TIMEOUT}s waiting for sync"; exit 1
