#!/usr/bin/env bash
# THE PIPELINE. Exactly what CI runs, runnable by hand so you can watch each step.
#   1. terraform apply        -> for_each creates ALL synced tables in config/tables.json
#   1b. generate changelogs   -> one Liquibase changelog PER table from config/tables.json
#   then FOR EACH table in config/tables.json:
#   2. wait_for_sync          -> block until the initial load is ONLINE (so indexes come after)
#   3. liquibase update       -> that table's OWN changelog: app role + grants + indexes + view
#   4. verify                 -> asserts app role can SELECT the consumer view + indexes exist;
#                                 exits NON-ZERO (fails the pipeline) if any post-condition is unmet
#
# Single source of truth: config/tables.json (also read by terraform/main.tf and the changelog
# generator). Add a table there and it is provisioned AND migrated by this one script — no code
# changes. Each table gets its OWN generated changelog (liquibase/generated/<name>.changelog.sql),
# so two tables sharing one app_schema get distinct changeset identities and never collide in a
# shared DATABASECHANGELOG (the failure the old shared-changelog + property-substitution had).
#
# Set the shared environment variables below for your workspace (or export them beforehand).
set -euo pipefail

PROFILE="${PROFILE:-DEFAULT}"
INSTANCE="${INSTANCE:-my-lakebase-project}"
HOST="${HOST:?set HOST to your Lakebase read-write endpoint host}"
PGUSER="${PGUSER:?set PGUSER to your Databricks username (email)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/config/tables.json"

# Step 4 "verify" lives in its own file so it can assert real post-conditions and be tested against
# a real Docker Postgres without a full terraform+liquibase run (scripts/tests/*verify*).
# shellcheck source=scripts/verify_table.sh
. "$ROOT/scripts/verify_table.sh"

echo "== 1. terraform apply (for_each creates all tables in config/tables.json) =="
( cd "$ROOT/terraform" && terraform init -input=false >/dev/null && terraform apply -auto-approve -input=false )

echo "== 1b. generate per-table Liquibase changelogs from config/tables.json =="
# One changelog per table (values baked in, no property substitution) -> a unique changeset
# identity per table, so shared-schema tables never collide in one DATABASECHANGELOG.
python3 "$ROOT/liquibase/generate_changelogs.py"

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
def emit(k, v): print(f"{k}={shlex.quote(str(v))}")
emit("T_NAME",   t["name"])
emit("T_STID",   t["synced_table_id"])
emit("T_SCHEMA", t["app_schema"])
emit("T_ROLE",   t["app_role"])
# index columns as a shell array (0/1/N), each shlex-quoted -> read straight into T_IDX=(...)
cols = t.get("index_columns") or []
print("T_IDX=(" + " ".join(shlex.quote(str(c)) for c in cols) + ")")
PY
)"
  PG_TABLE="${T_STID##*.}"   # last part of the 3-part UC name = Postgres table name
  echo ""
  echo "########## table '$T_NAME' ($T_STID) ##########"

  echo "== 2. wait for initial sync =="
  "$ROOT/scripts/wait_for_sync.sh" "$T_STID" "$PROFILE"

  echo "== 3. liquibase update (this table's OWN generated changelog) =="
  # Each table runs its OWN changelog (generated/<name>.changelog.sql), with role/schema/table and
  # ALL index columns baked in (no property substitution, no two-slot index cap). A distinct
  # changelog FILE per table => a distinct changeset identity, so two tables in ONE app_schema no
  # longer collide on 001-app-role in a shared DATABASECHANGELOG.
  ( cd "$ROOT/liquibase" && liquibase update \
      --changeLogFile="generated/$T_NAME.changelog.sql" \
      --url="$URL" --username="$PGUSER" --password="$TOKEN" \
      --liquibase-schema-name="$T_SCHEMA" )

  # Assert the real post-conditions and FAIL the pipeline if any is unmet: the app role can SELECT
  # its consumer view, and every expected index exists. verify_table returns non-zero on failure,
  # so `set -e` aborts here on the first table that does not verify -- the result is no longer
  # swallowed the way the old `2>&1`-and-ignore inline psql checks were.
  # ${#T_IDX[@]} guards the empty-array expansion for a table with zero index_columns (bash 3.2).
  if [ "${#T_IDX[@]}" -gt 0 ]; then
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "${PG_TABLE}_v" "${T_IDX[@]}"
  else
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "${PG_TABLE}_v"
  fi
done

echo ""
echo "== done: $COUNT table(s) provisioned + migrated =="
