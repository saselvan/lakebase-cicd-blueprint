#!/usr/bin/env bash
# Docker postgres:16 DOUBLE-APPLY proof for the DABs renderer (fix B) — the exact case that failed.
#
# The bug (verified on Docker PG16): the old alembic-rendered reconcile rolled the whole transaction
# back on the SECOND apply once a second Alembic revision existed — its unguarded `alembic_version`
# INSERT tripped `duplicate key value violates unique constraint "alembic_version_pkc"`, so the
# object DDL never reconciled. The Python renderer (dabs/render_ddl.py) emits NO version table, so a
# re-apply is a clean reconciling no-op.
#
# This is a SCRIPT, not a pytest test, so CI/pytest stay Docker-free (the offline `dabs-validate`
# gate needs no database). Run it locally / in a Docker-capable job:
#
#     bash dabs/tests/docker_double_apply_proof.sh
#
# What it proves against a real PostgreSQL 16:
#   1. render config/tables.json -> pipe to psql -> APPLY #1 succeeds.
#   2. simulate a synced-table replace (DROP the view + an index).
#   3. render again -> pipe to psql -> APPLY #2 succeeds with NO error (the case that used to roll
#      back), and the dropped view + index are RECONCILED back, and the app role/grant are present.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${CONFIG:-$REPO_ROOT/config/tables.json}"
PY="${PY:-python3}"
IMAGE="postgres:16"

command -v docker >/dev/null || { echo "ERROR: docker not found on PATH" >&2; exit 2; }

echo "--- starting throwaway $IMAGE ---"
CID="$(docker run -d --rm -e POSTGRES_PASSWORD=postgres "$IMAGE")"
cleanup() { docker stop "$CID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Wait until Postgres accepts connections. The official image boots a TEMP server for init and
# then restarts, so we require a real query to succeed (not just pg_isready, which can flap ready
# during the temp-server phase), and we poll generously for a cold init.
ready=""
for _ in $(seq 1 120); do
  if docker exec "$CID" psql -U postgres -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ -n "$ready" ] || { echo "ERROR: postgres never became ready" >&2; docker logs "$CID" 2>&1 | tail -20 >&2; exit 2; }

# Pipe SQL into the container's psql with ON_ERROR_STOP; a nonzero exit == a SQL error surfaced.
psql_run() { docker exec -i "$CID" psql -v ON_ERROR_STOP=1 -U postgres -d postgres -tA; }

# --- 0) base tables the migration grants on / builds a view over (derived from the config) ------
# Each table gets its app_schema + a base table carrying its PK column and every index column, so
# the rendered grants/indexes/view resolve. Derived from config so it never drifts from tables.json.
echo "--- creating base schemas + tables from $CONFIG ---"
"$PY" - "$CONFIG" <<'PY' | psql_run
import json, sys
tables = json.load(open(sys.argv[1]))
out = []
for t in tables:
    schema = t["app_schema"]
    tbl = t["synced_table_id"].split(".")[-1]
    cols = {t.get("primary_key_columns", ["id"])[0]: "int"}
    for c in t.get("index_columns", []):
        cols.setdefault(c, "text")
    coldefs = ", ".join(f"{name} {typ}" for name, typ in cols.items())
    out.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{tbl} ({coldefs});")
print("\n".join(out))
PY
echo "base tables created."

render() { "$PY" -m dabs.render_ddl --config "$CONFIG"; }
( cd "$REPO_ROOT" && render >/tmp/lkb_render_ddl.sql )
echo "--- rendered $(grep -c ';' /tmp/lkb_render_ddl.sql) statements (no alembic_version below) ---"
if grep -qi "alembic_version" /tmp/lkb_render_ddl.sql; then
  echo "ERROR: rendered SQL contains alembic_version — fix B regressed" >&2; exit 1
fi

echo "--- APPLY #1 ---"
psql_run </tmp/lkb_render_ddl.sql
echo "apply #1 OK"

# --- simulate a synced-table replace wiping the reconciled objects (the live scenario) ----------
echo "--- simulate a replace: DROP a view + an index ---"
FIRST_SCHEMA="$("$PY" -c "import json;t=json.load(open('$CONFIG'))[0];print(t['app_schema'])")"
FIRST_TBL="$("$PY" -c "import json;t=json.load(open('$CONFIG'))[0];print(t['synced_table_id'].split('.')[-1])")"
FIRST_IDXCOL="$("$PY" -c "import json;t=json.load(open('$CONFIG'))[0];print((t.get('index_columns') or [''])[0])")"
psql_run <<SQL
DROP VIEW IF EXISTS ${FIRST_SCHEMA}.${FIRST_TBL}_v;
DROP INDEX IF EXISTS ${FIRST_SCHEMA}.idx_${FIRST_TBL}_${FIRST_IDXCOL};
SQL
echo "wiped ${FIRST_SCHEMA}.${FIRST_TBL}_v and its index."

echo "--- APPLY #2 (the case that used to ROLL BACK) ---"
psql_run </tmp/lkb_render_ddl.sql
echo "apply #2 OK — no rollback."

# --- assert the wiped objects reconciled back AND the app role/grant survive --------------------
# The LAST column is the least-privilege proof: the app role can SELECT the consumer VIEW but must
# NOT hold SELECT on the base synced table (a Postgres view checks the base-table privilege as the
# view OWNER, not the caller, so the role never needs it — and granting it would let the role bypass
# the row-filterable view). So we require the base-table privilege to be 'f'.
FIRST_ROLE="$("$PY" -c "import json;t=json.load(open('$CONFIG'))[0];print(t['app_role'])")"
echo "--- verifying reconciled state (incl. least-privilege negative) ---"
RESULT="$(psql_run <<SQL
SELECT
  to_regclass('${FIRST_SCHEMA}.${FIRST_TBL}_v') IS NOT NULL,
  to_regclass('${FIRST_SCHEMA}.idx_${FIRST_TBL}_${FIRST_IDXCOL}') IS NOT NULL,
  EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${FIRST_ROLE}'),
  has_table_privilege('${FIRST_ROLE}', '${FIRST_SCHEMA}.${FIRST_TBL}_v', 'SELECT'),
  has_table_privilege('${FIRST_ROLE}', '${FIRST_SCHEMA}.${FIRST_TBL}', 'SELECT');
SQL
)"
echo "view_present|index_present|role_present|role_can_select_view|role_can_select_base = $RESULT"
# view+index reconciled, role present, role CAN read the view (t), role CANNOT read the base table (f).
if [ "$RESULT" != "t|t|t|t|f" ]; then
  echo "ERROR: 2nd apply did not reconcile to the least-privilege state (expected t|t|t|t|f, got $RESULT)" >&2
  echo "       (final 'f' is the base-table SELECT: it MUST be denied — the role reads only the view.)" >&2
  exit 1
fi

echo
echo "DOUBLE-APPLY PROOF OK: 2nd apply clean (no rollback); view+index reconciled; role can SELECT"
echo "the VIEW but is DENIED SELECT on the base table (least privilege)."
