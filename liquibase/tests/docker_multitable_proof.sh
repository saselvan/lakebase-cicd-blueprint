#!/usr/bin/env bash
# STATE-LEVEL proof for FIX A against a real Docker postgres:16 (no cloud, no Lakebase needed).
#
# Reproduces the exact live-broken case — TWO synced tables in ONE shared app_schema, one shared
# DATABASECHANGELOG — and proves the per-table generated changelogs let BOTH tables' migrations
# apply cleanly (both roles created, both consumer views readable). The OLD shared-changelog path
# tripped ValidationFailedException on the second table's `001-app-role` and never created its role.
#
# It also proves a config `view_name` OVERRIDE flows end-to-end: one stand-in table sets a stable
# consumer-view name distinct from the derived `<tbl>_v`, and the generated changelog creates THAT
# view (the derived name is never created).
#
# pytest stays Docker-free (that is what the offline suite asserts); CI runs this script in the
# `docker-proofs` job of .github/workflows/ci.yml. It can also be run by hand locally:
#     bash liquibase/tests/docker_multitable_proof.sh
#
# Requires: docker, liquibase, psql on PATH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="cicd_proj"                 # both tables share this schema (the broken case)
# One stand-in table gets a stable consumer-view name via config `view_name`, distinct from the
# derived providers_v — proving the override is honored by the generated changelog.
PROVIDERS_VIEW="providers_stable_v"
PORT=$(( (RANDOM % 20000) + 15000 ))
CID=""
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/lkb_multitable.XXXXXX")"

cleanup() {
  [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR" || true
}
trap cleanup EXIT

echo "== build a fixture config: two shared-schema tables, providers with a view_name OVERRIDE =="
FIXTURE_CONFIG="$WORKDIR/tables.json"
python3 - "$REPO_ROOT/config/tables.json" "$FIXTURE_CONFIG" "$PROVIDERS_VIEW" <<'PY'
import json, sys
src, dst, providers_view = sys.argv[1], sys.argv[2], sys.argv[3]
tables = json.load(open(src))
for t in tables:
    if t["name"] == "providers":
        t["view_name"] = providers_view   # override: distinct from the derived providers_v
json.dump(tables, open(dst, "w"), indent=2)
PY

echo "== generate per-table changelogs from the fixture config into $WORKDIR/generated =="
GEN="$WORKDIR/generated"
python3 "$REPO_ROOT/liquibase/generate_changelogs.py" --config "$FIXTURE_CONFIG" --out "$GEN" >/dev/null

echo "== start postgres:16 on :$PORT =="
CID=$(docker run -d --rm -e POSTGRES_PASSWORD=pw -p "127.0.0.1:$PORT:5432" postgres:16)
export PGHOST=127.0.0.1 PGPORT="$PORT" PGDATABASE=postgres PGUSER=postgres PGPASSWORD=pw
for _ in $(seq 1 60); do pg_isready -q && break; sleep 0.5; done

echo "== stand in for two synced tables in ONE schema ($SCHEMA) =="
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE SCHEMA IF NOT EXISTS $SCHEMA;" \
  -c "CREATE TABLE IF NOT EXISTS $SCHEMA.members   (id bigint primary key, member_id text, plan_code text);" \
  -c "CREATE TABLE IF NOT EXISTS $SCHEMA.providers (id bigint primary key, provider_id text, specialty_code text);"

URL="jdbc:postgresql://127.0.0.1:$PORT/postgres"
# Liquibase resolves --changeLogFile relative to cwd, so run from the fixture out-dir's parent and
# pass generated/<name>.changelog.sql (as deploy.sh does from liquibase/). logicalFilePath is baked
# as generated/<name>.changelog.sql in each header, so identity is stable regardless.
cd "$WORKDIR"
run_cl() { # $1 = table name; runs that table's OWN generated changelog (from the fixture out-dir).
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
check "members_app_ro can read members_v (derived name)" "SELECT has_table_privilege('members_app_ro','$SCHEMA.members_v','SELECT')"
# view_name OVERRIDE: providers' consumer view is the OVERRIDDEN name, readable by its role, and the
# DERIVED providers_v was NEVER created (the override, not the default, is what the changelog built).
check "providers_app_ro can read $PROVIDERS_VIEW (OVERRIDE)" "SELECT has_table_privilege('providers_app_ro','$SCHEMA.$PROVIDERS_VIEW','SELECT')"
check "derived providers_v does NOT exist (override honored)" "SELECT NOT EXISTS(SELECT 1 FROM pg_views WHERE schemaname='$SCHEMA' AND viewname='providers_v')"
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
  echo "VERDICT: PASS — two shared-schema tables both migrated cleanly; both roles + views exist;"
  echo "the providers view_name override was honored (derived providers_v absent)."
else
  echo "VERDICT: FAIL — see FAIL lines above."; exit 1
fi
