#!/usr/bin/env bash
# STATE-LEVEL proof against a real Docker postgres:16 (no cloud): a changed consumer view applies on
# the next deploy. 004-app-view is runAlways:true only. Liquibase reruns a runAlways changeset on
# every update and records its new checksum (exectype RERAN), so a changed 004 body applies with no
# validation error and no runOnChange. Checked live on a Lakebase branch on 2026-10-15 with
# Liquibase 4.33.0 (004 RERAN; the edited view returned 1 row). This proof keeps that behavior from regressing.
#
# Run by hand or in CI's docker-proofs job:  bash liquibase/tests/docker_view_edit_proof.sh
# Requires: docker, liquibase, psql on PATH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="cicd_proj"
PORT=$(( (RANDOM % 20000) + 15000 ))
CID=""
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/lkb_viewedit.XXXXXX")"
cleanup() { [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true; rm -rf "$WORKDIR" || true; }
trap cleanup EXIT

echo "== generate the members changelog from the repo config =="
python3 - "$REPO_ROOT/config/tables.json" "$WORKDIR/tables.json" <<'PY'
import json, sys
tables = [t for t in json.load(open(sys.argv[1])) if t["name"] == "members"]
json.dump(tables, open(sys.argv[2], "w"), indent=2)
PY
python3 "$REPO_ROOT/liquibase/generate_changelogs.py" --config "$WORKDIR/tables.json" --out "$WORKDIR/generated" >/dev/null
CL="$WORKDIR/generated/members.changelog.sql"

echo "== start postgres:16 on :$PORT =="
CID=$(docker run -d --rm -e POSTGRES_PASSWORD=pw -p "127.0.0.1:$PORT:5432" postgres:16)
export PGHOST=127.0.0.1 PGPORT="$PORT" PGDATABASE=postgres PGUSER=postgres PGPASSWORD=pw
for _ in $(seq 1 60); do psql -qtAc "SELECT 1" >/dev/null 2>&1 && break; sleep 0.5; done
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE SCHEMA IF NOT EXISTS $SCHEMA;" \
  -c "CREATE TABLE $SCHEMA.members (id bigint primary key, member_id text, plan_code text);" \
  -c "INSERT INTO $SCHEMA.members VALUES (1,'m1','A'),(2,'m2','B'),(3,'m3','A');"

URL="jdbc:postgresql://127.0.0.1:$PORT/postgres"
cd "$WORKDIR"
run_cl() { liquibase update --changeLogFile="generated/members.changelog.sql" \
  --url="$URL" --username="$PGUSER" --password="$PGPASSWORD" --liquibase-schema-name="$SCHEMA"; }

echo ""; echo "===== DEPLOY 1: original view ====="
run_cl

echo ""; echo "===== EDIT the view: hide rows (plan_code = 'A' only) ====="
python3 - "$CL" "$SCHEMA" <<'PY'
import sys
p, schema = sys.argv[1], sys.argv[2]
t = open(p).read()
old = f"SELECT * FROM {schema}.members;"
assert t.count(old) == 1, "view body not found"
open(p, "w").write(t.replace(old, f"SELECT * FROM {schema}.members WHERE plan_code = 'A';"))
PY

echo ""; echo "===== DEPLOY 2: changed 004 body ====="
rc=0; run_cl || rc=$?

echo ""; echo "===== STATE ASSERTIONS ====="
fail=0
[ "$rc" -eq 0 ] && echo "  OK   deploy 2 exited 0" || { echo "  FAIL deploy 2 exited $rc (checksum validation?)"; fail=1; }
rows=$(psql -tAc "SELECT count(*) FROM $SCHEMA.members_v" | tr -d '[:space:]')
[ "$rows" = "2" ] && echo "  OK   members_v shows the edited view (2 rows)" || { echo "  FAIL members_v rows = '$rows', expected 2"; fail=1; }
grant=$(psql -tAc "SELECT has_table_privilege('members_app_ro','$SCHEMA.members_v','SELECT')" | tr -d '[:space:]')
[ "$grant" = "t" ] && echo "  OK   members_app_ro can still SELECT members_v" || { echo "  FAIL members_app_ro SELECT = '$grant'"; fail=1; }

[ "$fail" -eq 0 ] && echo "VIEW EDIT PROOF PASSED" || { echo "VIEW EDIT PROOF FAILED"; exit 1; }
