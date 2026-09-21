#!/usr/bin/env bash
# THE PIPELINE. Exactly what CI runs, runnable by hand so you can watch each step.
#   1. terraform apply        -> for_each creates ALL synced tables in config/tables.json
#   1b. check changelog drift -> verify committed changelogs match config; abort deploy on drift
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
# Set the shared environment variables below for your workspace (or export them beforehand):
#   PROFILE  Databricks CLI profile                              (default DEFAULT)
#   PROJECT  Lakebase project id                                 (default my-lakebase-project)
#   BRANCH   branch to deploy to, within that project            (default main)
#   PGUSER   your Databricks username (email)                    (required)
# The deploy is BRANCH-DRIVEN: it resolves projects/$PROJECT/branches/$BRANCH to that branch's
# READ_WRITE compute endpoint and mints a runtime OAuth token FROM that endpoint (projects /
# Autoscaling API). There is no database-instance name and no manual HOST -- the host is derived
# from the resolved endpoint, so nothing can point the deploy at the production branch by mistake.
set -euo pipefail

PROFILE="${PROFILE:-DEFAULT}"
PROJECT="${PROJECT:-my-lakebase-project}"
BRANCH="${BRANCH:-main}"
PGUSER="${PGUSER:?set PGUSER to your Databricks username (email)}"
LAKEBASE_BRANCH="projects/$PROJECT/branches/$BRANCH"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG="$ROOT/config/tables.json"

# Shared resolve+mint: select the branch's READ_WRITE endpoint and mint an OAuth token FROM it.
# shellcheck source=scripts/resolve_endpoint.sh
. "$ROOT/scripts/resolve_endpoint.sh"

# Step 4 "verify" lives in its own file so it can assert real post-conditions and be tested against
# a real Docker Postgres without a full terraform+liquibase run (scripts/tests/*verify*).
# shellcheck source=scripts/verify_table.sh
. "$ROOT/scripts/verify_table.sh"

echo "== 1. terraform apply (for_each creates all tables in config/tables.json) =="
( cd "$ROOT/terraform" && terraform init -input=false >/dev/null && terraform apply -auto-approve -input=false )

echo "== 1b. verify committed per-table Liquibase changelogs are in sync with config/tables.json =="
# Do NOT regenerate at deploy time — that could apply something other than the reviewed, committed
# artifacts. Instead VERIFY the committed liquibase/generated/*.changelog.sql byte-match a fresh
# generation from config/tables.json, and ABORT the deploy (non-zero under `set -e`) on any drift.
# Regenerate + commit is a reviewed change, done before deploy; the deploy applies exactly that.
python3 "$ROOT/liquibase/generate_changelogs.py" --check

# Iterate every table entry in the single source of truth.
COUNT=$(python3 -c "import json;print(len(json.load(open('$CONFIG'))))")
for (( i = 0; i < COUNT; i++ )); do
  # Extract this table's fields as shell-safe assignments (shlex-quoted). T_VIEW is the RESOLVED
  # consumer-view name (config `view_name` override, else derived <pg_table>_v) — emitted via the
  # ONE shared seam dabs.render_ddl.resolve_view_name, the exact function both generators use, so
  # verify (step 4) looks for the SAME view the changelog created. Repo root goes on sys.path
  # (passed as the 3rd arg) exactly as liquibase/generate_changelogs.py does; the import is stdlib-only.
  eval "$(python3 - "$CONFIG" "$i" "$ROOT" <<'PY'
import json, sys, shlex
sys.path.insert(0, sys.argv[3])
from dabs.render_ddl import resolve_view_name
t = json.load(open(sys.argv[1]))[int(sys.argv[2])]
def emit(k, v): print(f"{k}={shlex.quote(str(v))}")
emit("T_NAME",   t["name"])
emit("T_STID",   t["synced_table_id"])
emit("T_SCHEMA", t["app_schema"])
emit("T_ROLE",   t["app_role"])
pg_table = str(t["synced_table_id"]).split(".")[-1]   # last part of the 3-part UC name
emit("T_VIEW",   resolve_view_name(t, pg_table))      # override or derived <pg_table>_v (shared seam)
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

  # Resolve the branch's READ_WRITE endpoint and mint a short-lived OAuth token FROM it INSIDE the
  # loop, AFTER the wait: each table's wait_for_sync can block for ~30 min, so a token minted once
  # before the loop could expire mid-run. Minting per table is cheap and short-lived; host + token
  # both come from the one resolved endpoint. Re-export PG* / URL from the fresh LB_HOST / LB_TOKEN.
  echo "== resolve branch READ_WRITE endpoint + mint runtime Lakebase credential =="
  lakebase_resolve_and_mint "$LAKEBASE_BRANCH" "$PROFILE"
  HOST="$LB_HOST"
  TOKEN="$LB_TOKEN"
  echo "  branch=$LAKEBASE_BRANCH host=$HOST endpoint=$LB_ENDPOINT"
  URL="jdbc:postgresql://$HOST:5432/databricks_postgres?sslmode=require"
  export PGHOST="$HOST" PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require PGUSER="$PGUSER" PGPASSWORD="$TOKEN"

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
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "$T_VIEW" "${T_IDX[@]}"
  else
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "$T_VIEW"
  fi
done

echo ""
echo "== done: $COUNT table(s) provisioned + migrated =="
