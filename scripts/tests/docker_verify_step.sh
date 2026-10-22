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
pos_out="$(verify_table "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code)"
printf '%s\n' "$pos_out"
if printf '%s\n' "$pos_out" | grep -q "PASS role $ROLE is denied on synced table $SCHEMA.$TABLE"; then
  echo "  OK   verify_table executed dabs/checks.py check list including base-table denial"
else
  echo "  FAIL verify_table did not emit the base-table denial check from dabs/checks.py"; fail=1
fi
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

echo ""; echo "===== NEGATIVE: direct GRANT SELECT on base table to app role ====="
psql -v ON_ERROR_STOP=1 -q -c "GRANT SELECT ON $SCHEMA.$TABLE TO $ROLE;"
expect nonzero "direct base-table SELECT grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code
psql -v ON_ERROR_STOP=1 -q -c "REVOKE ALL ON $SCHEMA.$TABLE FROM $ROLE;"

echo ""; echo "===== NEGATIVE: column-level GRANT SELECT (member_id) on base table ====="
psql -v ON_ERROR_STOP=1 -q -c "GRANT SELECT (member_id) ON $SCHEMA.$TABLE TO $ROLE;"
expect nonzero "column-level base-table SELECT grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code
psql -v ON_ERROR_STOP=1 -q -c "REVOKE ALL (member_id) ON $SCHEMA.$TABLE FROM $ROLE;"

echo ""; echo "===== NEGATIVE: GRANT SELECT on base table to PUBLIC ====="
psql -v ON_ERROR_STOP=1 -q -c "GRANT SELECT ON $SCHEMA.$TABLE TO PUBLIC;"
expect nonzero "PUBLIC base-table SELECT grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code
psql -v ON_ERROR_STOP=1 -q -c "REVOKE ALL ON $SCHEMA.$TABLE FROM PUBLIC;"

echo ""; echo "===== NEGATIVE: inherited role has SELECT on base table ====="
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE ROLE helper_inh NOLOGIN; GRANT SELECT ON $SCHEMA.$TABLE TO helper_inh; GRANT helper_inh TO $ROLE;"
expect nonzero "inherited base-table SELECT grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code
psql -v ON_ERROR_STOP=1 -q \
  -c "REVOKE helper_inh FROM $ROLE; REVOKE ALL ON $SCHEMA.$TABLE FROM helper_inh; DROP ROLE helper_inh;"

echo ""; echo "===== NEGATIVE: NOINHERIT role membership has SELECT on base table (SET ROLE route) ====="
psql -v ON_ERROR_STOP=1 -q \
  -c "ALTER ROLE $ROLE NOINHERIT; CREATE ROLE helper_noinh NOLOGIN; GRANT SELECT ON $SCHEMA.$TABLE TO helper_noinh; GRANT helper_noinh TO $ROLE;"
expect nonzero "NOINHERIT base-table SELECT grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code
psql -v ON_ERROR_STOP=1 -q \
  -c "REVOKE helper_noinh FROM $ROLE; REVOKE ALL ON $SCHEMA.$TABLE FROM helper_noinh; DROP ROLE helper_noinh; ALTER ROLE $ROLE INHERIT;"

echo ""; echo "===== NEGATIVE: dabs/checks.py CLI exits non-zero or prints 0 checks (fail closed) ====="
stub_dir="$(mktemp -d "${TMPDIR:-/tmp}/verify_cli_stub.XXXXXX")"
cat > "$stub_dir/python3" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$stub_dir/python3"
rc_cli_err=0
PATH="$stub_dir:$PATH" run_verify "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code || rc_cli_err=$?
cat > "$stub_dir/python3" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$stub_dir/python3"
rc_cli_zero=0
PATH="$stub_dir:$PATH" run_verify "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code || rc_cli_zero=$?
rm -rf "$stub_dir"
if [ "$rc_cli_err" -ne 0 ] && [ "$rc_cli_zero" -ne 0 ]; then
  echo "  OK   verify_table fails closed when CLI errors (rc=$rc_cli_err) or prints 0 checks (rc=$rc_cli_zero)"
else
  echo "  FAIL verify_table did not fail closed (rc_cli_err=$rc_cli_err, rc_cli_zero=$rc_cli_zero)"; fail=1
fi

echo ""; echo "===== NEGATIVE: SELECT on the consumer view REVOKED ====="
psql -v ON_ERROR_STOP=1 -q -c "REVOKE SELECT ON $SCHEMA.$VIEW FROM $ROLE;"
expect nonzero "missing grant -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== NEGATIVE: expected index DROPPED (grant restored) ====="
psql -v ON_ERROR_STOP=1 -q \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;" \
  -c "DROP INDEX IF EXISTS $SCHEMA.idx_${TABLE}_plan_code;"
expect nonzero "missing index -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== NEGATIVE: schema USAGE REVOKED (index restored) ====="
psql -v ON_ERROR_STOP=1 -q \
  -c "CREATE INDEX IF NOT EXISTS idx_${TABLE}_plan_code ON $SCHEMA.$TABLE (plan_code);" \
  -c "REVOKE USAGE ON SCHEMA $SCHEMA FROM $ROLE;"
expect nonzero "missing schema USAGE -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== NEGATIVE: view owner lost SELECT on the base table (rotation shape) ====="
# The live rotation spike (2026-10-14): after REASSIGN OWNED, the new owner of the view did not hold
# SELECT on the synced table. Every catalog check stayed 't', but a real read through the view
# failed with "permission denied for table members". Postgres checks the base table as the VIEW
# OWNER, so a view owned by a role without base SELECT reproduces it, even for a superuser caller.
psql -v ON_ERROR_STOP=1 -q \
  -c "GRANT USAGE ON SCHEMA $SCHEMA TO $ROLE;" \
  -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='new_view_owner') THEN CREATE ROLE new_view_owner NOLOGIN; END IF; END \$\$;" \
  -c "GRANT USAGE ON SCHEMA $SCHEMA TO new_view_owner;" \
  -c "ALTER VIEW $SCHEMA.$VIEW OWNER TO new_view_owner;" \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;"
catalog_ok="$(psql -tAc "SELECT has_table_privilege('$ROLE','$SCHEMA.$VIEW','SELECT')" | tr -d '[:space:]')"
if [ "$catalog_ok" = "t" ]; then
  echo "  OK   catalog check alone still says the app role can SELECT the view (t)"
else
  echo "  FAIL setup: expected the catalog check to stay t, got '$catalog_ok'"; fail=1
fi
expect nonzero "view owner without base SELECT -> non-zero exit" "$ROLE" "$SCHEMA" "$TABLE" "$VIEW" member_id plan_code

echo ""; echo "===== PR-VALIDATE WORKFLOW PATH: verify_table_from_row on config/tables.json row ====="
# Restore valid view ownership + least-privilege grants, then exercise the exact helper function
# (.github/workflows/pr-validate.yml -> verify_table_from_row "$ROW") against real Postgres 16.
psql -v ON_ERROR_STOP=1 -q \
  -c "ALTER VIEW $SCHEMA.$VIEW OWNER TO postgres;" \
  -c "GRANT USAGE ON SCHEMA $SCHEMA TO $ROLE;" \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;" \
  -c "REVOKE ALL ON $SCHEMA.$TABLE FROM $ROLE;"
MEMBERS_ROW="$(python3 -c "import json; print(json.dumps(json.load(open('$REPO_ROOT/config/tables.json'))[0], separators=(',',':')))")"
run_verify_row() { set +e; verify_table_from_row "$1"; local rc=$?; set -e; return "$rc"; }
expect_row() {
  local want="$1" label="$2" row="$3" rc=0
  run_verify_row "$row" || rc=$?
  if { [ "$want" = 0 ] && [ "$rc" -eq 0 ]; } || { [ "$want" = nonzero ] && [ "$rc" -ne 0 ]; }; then
    echo "  OK   $label (rc=$rc)"
  else
    echo "  FAIL $label (rc=$rc, wanted $want)"; fail=1
  fi
}
expect_row 0 "pr-validate verify_table_from_row: all post-conditions met -> exit 0" "$MEMBERS_ROW"

psql -v ON_ERROR_STOP=1 -q -c "REVOKE SELECT ON $SCHEMA.$VIEW FROM $ROLE;"
expect_row nonzero "pr-validate verify_table_from_row: missing view SELECT grant -> non-zero exit" "$MEMBERS_ROW"

psql -v ON_ERROR_STOP=1 -q \
  -c "GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;" \
  -c "GRANT SELECT ON $SCHEMA.$TABLE TO $ROLE;"
expect_row nonzero "pr-validate verify_table_from_row: base-table SELECT grant -> non-zero exit" "$MEMBERS_ROW"
psql -v ON_ERROR_STOP=1 -q -c "REVOKE ALL ON $SCHEMA.$TABLE FROM $ROLE;"

echo ""; echo "===== MUTATED CHANGELOG: GRANT SELECT on base table in 002 fails verify ====="
mut_dir="$(mktemp -d "${TMPDIR:-/tmp}/verify_mut_cl.XXXXXX")"
python3 - "$REPO_ROOT/liquibase/generated/members.changelog.sql" "$mut_dir/members.mutated.sql" <<'PY'
import sys
from pathlib import Path
src = Path(sys.argv[1]).read_text()
# Inject base-table SELECT grant into changeset 002 (no TABLE keyword — matches offline Attacker 3a)
target = "--changeset cicd:002-app-grants runAlways:true\n"
assert target in src
mut = src.replace(
    target,
    target + "GRANT SELECT ON cicd_proj.members TO members_app_ro;\n",
    1,
)
assert "GRANT SELECT ON cicd_proj.members TO members_app_ro;" in mut
Path(sys.argv[2]).write_text(mut)
PY
# Apply the active SQL of the mutated changelog against Docker PG16, then run verify_table_from_row:
grep -v '^--' "$mut_dir/members.mutated.sql" | psql -v ON_ERROR_STOP=1 -q >/dev/null
expect_row nonzero "mutated changelog with GRANT SELECT ON cicd_proj.members -> verify fails" "$MEMBERS_ROW"
psql -v ON_ERROR_STOP=1 -q -c "REVOKE ALL ON $SCHEMA.$TABLE FROM $ROLE;"
rm -rf "$mut_dir"

echo ""
if [ "$fail" = 0 ]; then
  echo "VERDICT: PASS — verify exits 0 only when the app role can read the view AND all indexes exist."
else
  echo "VERDICT: FAIL — see FAIL lines above."; exit 1
fi
