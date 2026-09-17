#!/usr/bin/env bash
# Safe schema-change testing on an ephemeral copy-on-write branch.
# A destructive migration on the branch leaves production untouched.
#
# Flow: create CoW branch -> connect to its endpoint -> run migration + validate ->
#       delete branch (or promote). Use this to test a drop/recreate-index migration or
#       any risky DDL WITHOUT touching production.
#
# Usage: branch_test.sh <project_id> <branch_id> [profile]
set -euo pipefail

PROJECT="${1:?project_id required (e.g. my-lakebase-project)}"
BRANCH="${2:?branch_id required (e.g. pr-123)}"
PROFILE="${3:-DEFAULT}"

echo "== create ephemeral branch (2h TTL, auto-cleanup) =="
databricks postgres create-branch "projects/$PROJECT" "$BRANCH" \
  --json '{"spec":{"ttl":"7200s"}}' -p "$PROFILE" >/dev/null

echo "== branch endpoint host =="
HOST=$(databricks postgres list-endpoints "projects/$PROJECT/branches/$BRANCH" -p "$PROFILE" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['status']['hosts']['host'])")
echo "  host=$HOST"

TOKEN=$(databricks database generate-database-credential \
  --json "{\"instance_names\":[\"$PROJECT\"]}" -p "$PROFILE" -o json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

export PGHOST="$HOST" PGPORT=5432 PGDATABASE=databricks_postgres PGSSLMODE=require
export PGUSER="$(databricks auth describe -p "$PROFILE" -o json | python3 -c "import sys,json;print(json.load(sys.stdin)['username'])")"
export PGPASSWORD="$TOKEN"

echo "== run your migration here against the BRANCH, then validate =="
# Example — a drop/recreate-index pattern, tested in isolation:
#   psql -c "DROP INDEX IF EXISTS cicd_proj.idx_members_plan_code;"
#   psql -c "CREATE INDEX CONCURRENTLY idx_members_plan_code ON cicd_proj.members (plan_code);"
#   psql -c "SELECT count(*) FROM cicd_proj.members;"   # validate

echo "== when done: delete the branch (production was never touched) =="
echo "   databricks postgres delete-branch projects/$PROJECT/branches/$BRANCH -p $PROFILE"
