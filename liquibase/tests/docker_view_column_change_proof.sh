#!/usr/bin/env bash
# STATE-LEVEL proof against a real Docker postgres:16 (no cloud):
# The consumer view survives base-table column changes (drop, rename, reorder, mid-insert) on BOTH
# the DABs path (`dabs/render_ddl.py` + `dabs/migration_job.py:apply_sql_to_lakebase`) and the
# Liquibase path (`liquibase/generate_changelogs.py` + `liquibase update`), with:
#   - Attacker 1: base table drops a column between deploy 1 and deploy 2 (both paths).
#   - Attacker 2: rename a column, reorder columns, and add a column in the middle (both paths).
#   - Attacker 3: a concurrent reader loop runs `SELECT` on the view as the app role during deploy 2
#                 and counts errors (0 missing-view and 0 permission-denied errors).
#   - Attacker 4: rebuild fails halfway (bad SQL injected in the same apply / changeset):
#                 the transaction rolls back so the old view and its SELECT grant stay intact.
#   - Attacker 5: DABs apply_sql_to_lakebase runs inside a single transaction (autocommit=False),
#                 never leaving a missing-view or ungranted-view gap between DROP and CREATE+GRANT.
#   - Attacker 6: a synced-table replace (`DROP TABLE <schema>.<table>`) while the consumer view is
#                 in place is still blocked by Postgres (`cannot drop table ... because other objects
#                 depend on it`).
#
# Run by hand or in CI's docker-proofs job:
#     bash liquibase/tests/docker_view_column_change_proof.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCHEMA="cicd_proj"
ROLE="members_app_ro"
TABLE="members"
VIEW="members_v"
PY="${PY:-python3}"
PORT=$(( (RANDOM % 20000) + 15000 ))
CID=""
WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/lkb_viewcol.XXXXXX")"

cleanup() {
  [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1 || true
  rm -rf "$WORKDIR" || true
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "ERROR: docker not found on PATH" >&2; exit 2; }

echo "== build single-table (members) fixture config and generate changelog + DABs DDL =="
"$PY" - "$REPO_ROOT/config/tables.json" "$WORKDIR/tables.json" <<'PY'
import json, sys
tables = [t for t in json.load(open(sys.argv[1])) if t["name"] == "members"]
json.dump(tables, open(sys.argv[2], "w"), indent=2)
PY

"$PY" "$REPO_ROOT/liquibase/generate_changelogs.py" --config "$WORKDIR/tables.json" --out "$WORKDIR/generated" >/dev/null
( cd "$REPO_ROOT" && "$PY" -m dabs.render_ddl --config "$WORKDIR/tables.json" > "$WORKDIR/dabs_render.sql" )

echo "== start postgres:16 on :$PORT =="
CID="$(docker run -d --rm -e POSTGRES_PASSWORD=pw -p "127.0.0.1:$PORT:5432" postgres:16)"
export PGHOST=127.0.0.1 PGPORT="$PORT" PGDATABASE=postgres PGUSER=postgres PGPASSWORD=pw

ready=""
for _ in $(seq 1 120); do
  if docker exec "$CID" psql -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 0.5
done
[ -n "$ready" ] || { echo "ERROR: postgres never became ready" >&2; exit 2; }

psql_super() {
  docker exec -i "$CID" psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -U postgres -d postgres -tA "$@"
}

# Run Liquibase either via host binary (if installed) or via the official liquibase:4.33 container.
run_liquibase_update() {
  local changelog_rel="$1"
  if command -v liquibase >/dev/null 2>&1; then
    (
      cd "$WORKDIR"
      liquibase update \
        --changeLogFile="$changelog_rel" \
        --url="jdbc:postgresql://127.0.0.1:$PORT/postgres" \
        --username="$PGUSER" \
        --password="$PGPASSWORD" \
        --liquibase-schema-name="$SCHEMA" >/dev/null 2>&1
    )
  else
    docker run --rm --network="container:$CID" \
      -v "$WORKDIR:/work" \
      liquibase/liquibase:4.33 \
      --search-path=/work \
      --changeLogFile="$changelog_rel" \
      --url="jdbc:postgresql://127.0.0.1:5432/postgres" \
      --username="$PGUSER" \
      --password="$PGPASSWORD" \
      --liquibase-schema-name="$SCHEMA" \
      update >/dev/null 2>&1
  fi
}

# Run DABs apply_sql_to_lakebase against the real Postgres 16 container (bridging psycopg.connect
# to psql inside the container with the exact autocommit setting apply_sql_to_lakebase passes).
run_dabs_apply_file() {
  local sql_file="$1"
  "$PY" - "$REPO_ROOT" "$sql_file" "$CID" <<'PY'
import subprocess, sys, types
from pathlib import Path

repo_root = Path(sys.argv[1])
sql_text = Path(sys.argv[2]).read_text()
cid = sys.argv[3]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from dabs import migration_job as mj

fake_psycopg = types.ModuleType("psycopg")

class _Cur:
    def __init__(self, conn):
        self._conn = conn
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, sql):
        self._conn.stmts.append(sql)
        if self._conn.autocommit:
            # Autocommit mode: execute each statement outside a transaction block
            proc = subprocess.run(
                ["docker", "exec", "-i", cid, "psql", "-h", "127.0.0.1", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-tA"],
                input=sql, text=True, capture_output=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)

class _Conn:
    def __init__(self, autocommit):
        self.autocommit = autocommit
        self.stmts = []
        self.committed = False
    def __enter__(self):
        return self
    def __exit__(self, exc_type, *a):
        if exc_type is None and not self.autocommit:
            full_sql = "\n".join(self.stmts)
            proc = subprocess.run(
                ["docker", "exec", "-i", cid, "psql", "-h", "127.0.0.1", "-v", "ON_ERROR_STOP=1", "--single-transaction", "-U", "postgres", "-d", "postgres", "-tA"],
                input=full_sql, text=True, capture_output=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr)
            self.committed = True
        return False
    def cursor(self):
        return _Cur(self)

conns = []
def _connect(conninfo, autocommit=False):
    c = _Conn(autocommit)
    conns.append(c)
    return c

fake_psycopg.connect = _connect
sys.modules["psycopg"] = fake_psycopg
mj._lakebase_conninfo = lambda **kw: "host=127.0.0.1 port=5432 dbname=postgres user=postgres password=pw sslmode=disable"

mj.apply_sql_to_lakebase(sql_text, branch="projects/p/branches/b")
if not conns or conns[0].autocommit is not False or not conns[0].committed or len(conns[0].stmts) != 1:
    sys.stderr.write(
        f"FAIL: apply_sql_to_lakebase must run in a single non-autocommit transaction "
        f"(autocommit={conns[0].autocommit if conns else None}, stmts={len(conns[0].stmts) if conns else 0})\n"
    )
    sys.exit(1)
PY
}

seed_deploy_sql() {
  local path_name="$1"
  if [ "$path_name" = "dabs" ]; then
    docker exec -i "$CID" psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -U postgres -d postgres -tA < "$WORKDIR/dabs_render.sql" >/dev/null
  else
    run_liquibase_update "generated/members.changelog.sql"
  fi
}

reset_base_table() {
  psql_super <<SQL >/dev/null
DROP SCHEMA IF EXISTS $SCHEMA CASCADE;
CREATE SCHEMA $SCHEMA;
CREATE TABLE $SCHEMA.$TABLE (
  id bigint primary key,
  member_id text,
  plan_code text,
  legacy_col text
);
INSERT INTO $SCHEMA.$TABLE VALUES (1, 'm1', 'A', 'old1'), (2, 'm2', 'B', 'old2');
SQL
}

check_view_and_grant() {
  local want_cols="$1"
  local got_cols got_priv got_base_priv got_rows
  got_cols="$(psql_super -c "SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns WHERE table_schema='$SCHEMA' AND table_name='$VIEW';")"
  got_priv="$(psql_super -c "SELECT has_table_privilege('$ROLE', '$SCHEMA.$VIEW', 'SELECT');")"
  got_base_priv="$(psql_super -c "SELECT has_table_privilege('$ROLE', '$SCHEMA.$TABLE', 'SELECT');")"
  got_rows="$(psql_super -c "SET ROLE $ROLE; SELECT count(*) FROM $SCHEMA.$VIEW;" | tail -n 1)"
  if [ "$got_cols" != "$want_cols" ] || [ "$got_priv" != "t" ] || [ "$got_base_priv" != "f" ] || [ "$got_rows" != "2" ]; then
    echo "  FAIL state check: cols='$got_cols' (want '$want_cols') view_select='$got_priv' (want t) base_select='$got_base_priv' (want f) rows='$got_rows' (want 2)"
    return 1
  fi
  return 0
}

fail=0

run_path_suite() {
  local path_name="$1" # "dabs" or "liquibase"
  echo ""
  echo "================================================================="
  echo "  PATH: $path_name"
  echo "================================================================="

  apply_deploy() {
    if [ "$path_name" = "dabs" ]; then
      run_dabs_apply_file "$WORKDIR/dabs_render.sql"
    else
      run_liquibase_update "generated/members.changelog.sql"
    fi
  }

  # --- Deploy 1: initial table (id, member_id, plan_code, legacy_col) ---
  reset_base_table
  set +e
  apply_deploy
  rc_d1=$?
  set -e
  if [ "$rc_d1" -eq 0 ] && check_view_and_grant "id,member_id,plan_code,legacy_col"; then
    echo "  OK   [$path_name] deploy 1 + Attacker 5 single-tx check passed"
  else
    echo "  FAIL [$path_name] deploy 1 / Attacker 5 single-tx check failed (rc=$rc_d1)"
    fail=1
    seed_deploy_sql "$path_name" || true
  fi

  # --- Attacker 6: fail-closed replace interlock while view is in place ---
  set +e
  drop_err="$(psql_super -c "DROP TABLE $SCHEMA.$TABLE;" 2>&1)"
  drop_rc=$?
  set -e
  if [ "$drop_rc" -ne 0 ] && printf '%s' "$drop_err" | grep -qi "cannot drop table.*because other objects depend on it"; then
    echo "  OK   [$path_name] Attacker 6: DROP TABLE blocked by dependent consumer view"
  else
    echo "  FAIL [$path_name] Attacker 6: DROP TABLE was not blocked (rc=$drop_rc, out=$drop_err)"
    fail=1
  fi

  # --- Attacker 2a: RENAME column on base table between deploy 1 and deploy 2 ---
  reset_base_table
  seed_deploy_sql "$path_name"
  psql_super -c "ALTER TABLE $SCHEMA.$TABLE RENAME COLUMN legacy_col TO renamed_col;" >/dev/null
  set +e
  apply_deploy
  rc_rename=$?
  set -e
  if [ "$rc_rename" -eq 0 ] && check_view_and_grant "id,member_id,plan_code,renamed_col"; then
    echo "  OK   [$path_name] Attacker 2a (rename column): deploy 2 succeeded and updated view columns"
  else
    echo "  FAIL [$path_name] Attacker 2a (rename column): deploy 2 failed (rc=$rc_rename)"
    fail=1
  fi

  # --- Attacker 1: DROP column on base table between deploy 1 and deploy 2 ---
  reset_base_table
  seed_deploy_sql "$path_name"
  psql_super <<SQL >/dev/null
CREATE OR REPLACE VIEW $SCHEMA.$VIEW AS
  SELECT id, member_id, plan_code, 'decoupled'::text AS legacy_col FROM $SCHEMA.$TABLE;
GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;
ALTER TABLE $SCHEMA.$TABLE DROP COLUMN legacy_col;
SQL
  set +e
  apply_deploy
  rc_drop=$?
  set -e
  if [ "$rc_drop" -eq 0 ] && check_view_and_grant "id,member_id,plan_code"; then
    echo "  OK   [$path_name] Attacker 1 (drop column): deploy 2 succeeded and dropped column from view"
  else
    echo "  FAIL [$path_name] Attacker 1 (drop column): deploy 2 failed (rc=$rc_drop)"
    fail=1
  fi

  # --- Attacker 2b: REORDER columns and ADD column in the middle ---
  reset_base_table
  seed_deploy_sql "$path_name"
  psql_super <<SQL >/dev/null
DROP VIEW IF EXISTS $SCHEMA.$VIEW;
CREATE VIEW $SCHEMA.$VIEW AS
  SELECT 1::bigint AS id, 'm1'::text AS member_id, 'A'::text AS plan_code;
GRANT SELECT ON $SCHEMA.$VIEW TO $ROLE;
DROP TABLE $SCHEMA.$TABLE;
CREATE TABLE $SCHEMA.$TABLE (
  id bigint primary key,
  mid_col text,
  plan_code text,
  member_id text
);
INSERT INTO $SCHEMA.$TABLE VALUES (1, 'mid1', 'A', 'm1'), (2, 'mid2', 'B', 'm2');
SQL
  set +e
  apply_deploy
  rc_reorder=$?
  set -e
  if [ "$rc_reorder" -eq 0 ] && check_view_and_grant "id,mid_col,plan_code,member_id"; then
    echo "  OK   [$path_name] Attacker 2b (reorder + mid-insert column): deploy 2 succeeded"
  else
    echo "  FAIL [$path_name] Attacker 2b (reorder + mid-insert column): deploy 2 failed (rc=$rc_reorder)"
    fail=1
  fi

  # --- Attacker 4: halfway failure rolls back so old view and its SELECT grant stay intact ---
  reset_base_table
  seed_deploy_sql "$path_name"
  if [ "$path_name" = "dabs" ]; then
    cat "$WORKDIR/dabs_render.sql" > "$WORKDIR/dabs_bad.sql"
    "$PY" - "$WORKDIR/dabs_bad.sql" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read()
target = "DROP VIEW IF EXISTS cicd_proj.members_v;"
if target in t:
    t = t.replace(target, target + "\nSELECT * FROM cicd_proj.__nonexistent_table_for_rollback_test__;")
else:
    t = "DROP VIEW IF EXISTS cicd_proj.members_v;\nSELECT * FROM cicd_proj.__nonexistent_table_for_rollback_test__;\n" + t
open(p, "w").write(t)
PY
    set +e
    run_dabs_apply_file "$WORKDIR/dabs_bad.sql" 2>/dev/null
    rc_bad=$?
    set -e
  else
    cp "$WORKDIR/generated/members.changelog.sql" "$WORKDIR/generated/members_bad.changelog.sql"
    "$PY" - "$WORKDIR/generated/members_bad.changelog.sql" <<'PY'
import sys
p = sys.argv[1]
t = open(p).read()
target = "GRANT SELECT ON cicd_proj.members_v"
t = t.replace(target, "SELECT * FROM cicd_proj.__nonexistent_table_for_rollback_test__;\n" + target)
open(p, "w").write(t)
PY
    set +e
    run_liquibase_update "generated/members_bad.changelog.sql"
    rc_bad=$?
    set -e
  fi
  if [ "$rc_bad" -ne 0 ] && check_view_and_grant "id,member_id,plan_code,legacy_col"; then
    echo "  OK   [$path_name] Attacker 4 (halfway failure): transaction rolled back; old view + grant intact"
  else
    echo "  FAIL [$path_name] Attacker 4 (halfway failure): view or grant lost after halfway failure (rc_bad=$rc_bad)"
    fail=1
  fi

  # --- Attacker 3 & 5: concurrent reader loop as app_role during deploy 2 sees 0 errors ---
  reset_base_table
  seed_deploy_sql "$path_name"
  stop_file="$WORKDIR/stop_reader_${path_name}"
  err_file="$WORKDIR/reader_errors_${path_name}.log"
  count_file="$WORKDIR/reader_count_${path_name}.txt"
  rm -f "$stop_file" "$err_file" "$count_file"
  : > "$err_file"

  (
    iters=0
    while [ ! -f "$stop_file" ]; do
      if ! docker exec -i "$CID" psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -U postgres -d postgres -tAc \
        "SET ROLE $ROLE; SELECT count(*) FROM $SCHEMA.$VIEW;" >/dev/null 2>>"$err_file"; then
        echo "READ_ERR_AT_$iters" >> "$err_file"
      fi
      iters=$((iters + 1))
    done
    echo "$iters" > "$count_file"
  ) &
  reader_pid=$!

  sleep 0.3
  set +e
  apply_deploy && apply_deploy && apply_deploy
  rc_concurrent=$?
  set -e
  touch "$stop_file"
  wait "$reader_pid" || true

  err_lines="$(wc -l < "$err_file" | tr -d '[:space:]')"
  reads_done="$(cat "$count_file" 2>/dev/null || echo 0)"
  if [ "$rc_concurrent" -eq 0 ] && [ "$err_lines" = "0" ] && [ "$reads_done" -gt 0 ]; then
    echo "  OK   [$path_name] Attacker 3 & 5 (concurrent reader): $reads_done reads, 0 errors during deploy"
  else
    echo "  FAIL [$path_name] Attacker 3 & 5 (concurrent reader): rc=$rc_concurrent, reads=$reads_done, errors=$err_lines"
    cat "$err_file" >&2 || true
    fail=1
  fi
}

run_path_suite dabs
run_path_suite liquibase

echo ""
if [ "$fail" -eq 0 ]; then
  echo "VIEW COLUMN CHANGE PROOF PASSED (both DABs and Liquibase paths)"
else
  echo "VIEW COLUMN CHANGE PROOF FAILED"
  exit 1
fi
