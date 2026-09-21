#!/usr/bin/env bash
# STATE-LEVEL proof for the deploy verify step against a real Docker postgres:16 (no cloud, no Lakebase needed).
#
# scripts/deploy.sh step 4 "verify" used to run its psql checks as `... 2>&1 || true` and never
# inspect the output, so a false post-condition (app role NOT granted SELECT on its consumer view,
# or a missing index) still printed and exited 0 — the guarantee the README/RUNBOOK sell could not
# fail. Step 4 is now scripts/verify_table.sh's `verify_table`, which asserts and exits non-zero.
#
# This reproduces both cases against real Postgres GRANT/REVOKE and real indexes:
#   POSITIVE  — role granted SELECT on the view + both indexes present   => verify exits 0
#   NEGATIVE  — same table but the SELECT grant on the view is REVOKED   => verify exits NON-ZERO
#   NEGATIVE  — grant present but an expected index is DROPPED           => verify exits NON-ZERO
#
# Not wired into pytest on purpose: the offline `pytest` suite (and CI) stay Docker-free. The
# Docker-free equivalent (stub psql) is scripts/tests/test_verify_step.py. Run this by hand:
#   bash scripts/tests/docker_verify_step.sh
#
# Requires: docker, psql on PATH.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="cicd_proj"
ROLE="members_app_ro"
TABLE="members"
VIEW="${TABLE}_v"
PORT=$(( (RANDOM % 20000) + 15000 ))
CID=""

cleanup() { [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "== start postgres:16 on :$PORT =="
CID=$(docker run -d --rm -e POSTGRES_PASSWORD=pw -p "127.0.0.1:$PORT:5432" postgres:16)
export PGHOST=127.0.0.1 PGPORT="$PORT" PGDATABASE=postgres PGUSER=postgres PGPASSWORD=pw
for _ in $(seq 1 60); do pg_isready -q && break; sleep 0.5; done

echo "== stand in for a synced table + the consumer view + role + indexes =="
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE SCHEMA IF NOT EXISTS $SCHEMA;" \
  -c "CREATE TABLE IF NOT EXISTS $SCHEMA.$TABLE (id bigint primary key, member_id text, plan_code text);" \
  -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='$ROLE') THEN CREATE ROLE $ROLE NOLOGIN; END IF; END \$\$;" \
  -c "CREATE OR REPLACE VIEW $SCHEMA.$VIEW AS SELECT * FROM $SCHEMA.$TABLE;" \
  -c "CREATE INDEX IF NOT EXISTS idx_${TABLE}_member_id ON $SCHEMA.$TABLE (member_id);" \
  -c "CREATE INDEX IF NOT EXISTS idx_${TABLE}_plan_code ON $SCHEMA.$TABLE (plan_code);"

# shellcheck source=scripts/verify_table.sh
. "$REPO_ROOT/scripts/verify_table.sh"

# Run verify_table without aborting this script's `set -e` on the negative cases; capture its rc.
run_verify() { set +e; verify_table "$@"; local rc=$?; set -e; return "$rc"; }

fail=0
expect() { # $1 = expected rc (0 or 'nonzero'); $2 = label; remaining = verify_table args
  local want="$1" label="$2"; shift 2
  local rc=0
  run_verify "$@" || rc=$?
  if { [ "$want" = 0 ] && [ "$rc" -eq 0 ]; } || { [ "$want" = nonzero ] && [ "$rc" -ne 0 ]; }; then
    echo "  OK   $label (rc=$rc)"
  else
    echo "  FAIL $label (rc=$rc, wanted $want)"; fail=1
  fi
}

echo ""; echo "===== POSITIVE: grant present + both indexes ====="
# Least-privilege grants exactly as the migration emits them: schema USAGE + SELECT on the VIEW
# only. The base-table SELECT is deliberately NOT granted (the role reads only through the view).
psql -v ON_ERROR_STOP=1 -q \
  -c "GRANT USAGE ON SCHEMA $SCHEMA TO $ROLE;" \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;"
expect 0 "all post-conditions met -> exit 0" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== LEAST PRIVILEGE: role reads the VIEW but is DENIED the base table ====="
# A Postgres view checks the base-table privilege as the view OWNER, not the caller, so the app
# role never needs SELECT on the base synced table — and must not have it (else it could bypass the
# row-filterable consumer view). Assert the base-table SELECT is 'f' while the view SELECT is 't'.
lp_base="$(psql -tAc "SELECT has_table_privilege('$ROLE','$SCHEMA.$TABLE','SELECT')" | tr -d '[:space:]')"
lp_view="$(psql -tAc "SELECT has_table_privilege('$ROLE','$SCHEMA.$VIEW','SELECT')" | tr -d '[:space:]')"
if [ "$lp_base" = "f" ] && [ "$lp_view" = "t" ]; then
  echo "  OK   role can SELECT the view (t) but NOT the base table (f) (base=$lp_base view=$lp_view)"
else
  echo "  FAIL least privilege violated (base=$lp_base view=$lp_view; want base=f view=t)"; fail=1
fi

echo ""; echo "===== NEGATIVE: SELECT on the consumer view REVOKED ====="
psql -v ON_ERROR_STOP=1 -q -c "REVOKE SELECT ON $SCHEMA.$VIEW FROM $ROLE;"
expect nonzero "missing grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== NEGATIVE: expected index DROPPED (grant restored) ====="
psql -v ON_ERROR_STOP=1 -q \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;" \
  -c "DROP INDEX IF EXISTS $SCHEMA.idx_${TABLE}_plan_code;"
expect nonzero "missing index -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""
if [ "$fail" = 0 ]; then
  echo "VERDICT: PASS — verify exits 0 only when the app role can read the view AND all indexes exist."
else
  echo "VERDICT: FAIL — see FAIL lines above."; exit 1
fi
