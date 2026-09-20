#!/usr/bin/env bash
# STATE-LEVEL proof for FIX A against a real Docker postgres:16 (no cloud, no Lakebase needed).
#
# Reproduces the exact live-broken case — TWO synced tables in ONE shared app_schema, one shared
# DATABASECHANGELOG — and proves the per-table generated changelogs let BOTH tables' migrations
# apply cleanly (both roles created, both consumer views readable). The OLD shared-changelog path
# tripped ValidationFailedException on the second table's `001-app-role` and never created its role.
#
# Not wired into pytest on purpose: the offline `pytest` suite must stay Docker-free (that is what
# CI runs). Run this by hand: `bash liquibase/tests/docker_multitable_proof.sh`.
#
# Requires: docker, liquibase, psql on PATH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GEN="$REPO_ROOT/liquibase/generated"
SCHEMA="cicd_proj"                 # both tables share this schema (the broken case)
PORT=$(( (RANDOM % 20000) + 15000 ))
CID=""

cleanup() { [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== start postgres:16 on :$PORT =="
CID=$(docker run -d --rm -e POSTGRES_PASSWORD=pw -p "127.0.0.1:$PORT:5432" postgres:16)
export PGHOST=127.0.0.1 PGPORT="$PORT" PGDATABASE=postgres PGUSER=postgres PGPASSWORD=pw
for _ in $(seq 1 60); do pg_isready -q && break; sleep 0.5; done

echo "== regenerate committed changelogs (single source of truth) =="
python3 "$REPO_ROOT/liquibase/generate_changelogs.py" >/dev/null

echo "== stand in for two synced tables in ONE schema ($SCHEMA) =="
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE SCHEMA IF NOT EXISTS $SCHEMA;" \
  -c "CREATE TABLE IF NOT EXISTS $SCHEMA.members   (id bigint primary key, member_id text, plan_code text);" \
  -c "CREATE TABLE IF NOT EXISTS $SCHEMA.providers (id bigint primary key, provider_id text, specialty_code text);"

URL="jdbc:postgresql://127.0.0.1:$PORT/postgres"
cd "$REPO_ROOT/liquibase"   # liquibase resolves --changeLogFile relative to cwd (as deploy.sh does)
run_cl() { # $1 = table name; runs generated/<name>.changelog.sql (that table's OWN changelog)
  liquibase update --changeLogFile="generated/$1.changelog.sql" \
    --url="$URL" --username="$PGUSER" --password="$PGPASSWORD" \
    --liquibase-schema-name="$SCHEMA"   # SHARED DATABASECHANGELOG in cicd_proj — the collision case
}

echo ""; echo "===== RUN 1: members.changelog.sql ====="
run_cl members
echo ""; echo "===== RUN 2: providers.changelog.sql (SAME schema, shared DATABASECHANGELOG) ====="
run_cl providers

echo ""; echo "===== STATE ASSERTIONS ====="
fail=0
check() { # $1 = label  $2 = psql expr expected to return 't'
  local got; got=$(psql -tAc "$2" 2>/dev/null | tr -d '[:space:]')
  if [ "$got" = "t" ]; then echo "  OK   $1"; else echo "  FAIL $1 (got '$got')"; fail=1; fi
}
check "role members_app_ro exists"    "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='members_app_ro')"
check "role providers_app_ro exists"  "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='providers_app_ro')"
check "members_app_ro can read members_v"     "SELECT has_table_privilege('members_app_ro','$SCHEMA.members_v','SELECT')"
check "providers_app_ro can read providers_v" "SELECT has_table_privilege('providers_app_ro','$SCHEMA.providers_v','SELECT')"
# Least privilege: the app role reads ONLY the consumer view, never the base synced table. A
# Postgres view checks the base-table privilege as the view OWNER, not the caller, so the role
# needs no base-table SELECT — and granting it would let the role bypass the row-filterable view.
# has_table_privilege(...,'SELECT') on the base table must be 'f'; NOT(...) == 't' proves it.
check "members_app_ro CANNOT read base members"     "SELECT NOT has_table_privilege('members_app_ro','$SCHEMA.members','SELECT')"
check "providers_app_ro CANNOT read base providers" "SELECT NOT has_table_privilege('providers_app_ro','$SCHEMA.providers','SELECT')"

echo "-- DATABASECHANGELOG (shared in $SCHEMA): filename | id | author:"
psql -tAF'|' -c "SELECT filename,id,author FROM $SCHEMA.databasechangelog ORDER BY orderexecuted;"

echo ""
if [ "$fail" = 0 ]; then
  echo "VERDICT: PASS — two shared-schema tables both migrated cleanly; both roles + views exist."
else
  echo "VERDICT: FAIL — see FAIL lines above."; exit 1
fi
