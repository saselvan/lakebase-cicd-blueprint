#!/usr/bin/env bash
# Docker postgres:16 DOUBLE-APPLY proof for the DABs renderer.
#
# The renderer (dabs/render_ddl.py) emits NO migration-version table: every statement is idempotent
# (guarded CREATE ROLE, idempotent GRANT, CREATE INDEX IF NOT EXISTS, CREATE OR REPLACE VIEW), so
# applying the rendered DDL a second time is a clean reconciling no-op with nothing to roll back.
# This proves that against a real Postgres. (Why a version-tracking variant was dropped:
# docs/adr/0006-dabs-variant-mechanics.md.)
#
# This is a SCRIPT, not a pytest test, so CI/pytest stay Docker-free (the offline `dabs-validate`
# gate needs no database). Run it locally / in a Docker-capable job:
#
#     bash dabs/tests/docker_double_apply_proof.sh
#
# What it proves against a real PostgreSQL 16:
#   1. Attacker 1 (role missing, deploy identity WITH CREATEROLE): apply fails non-zero, emits
#      "app role members_app_ro does not exist; an admin must create it", and pg_roles does NOT
#      contain members_app_ro afterwards.
#   2. Attacker 2 (role missing, deploy identity WITHOUT CREATEROLE): apply fails non-zero with the
#      same exact message "app role members_app_ro does not exist; an admin must create it" (not a
#      CREATEROLE permission error).
#   3. Attacker 3 (role present): admin pre-creates the app roles -> APPLY #1 succeeds (both WITH
#      and WITHOUT CREATEROLE).
#   4. simulate a synced-table replace (DROP the view + an index).
#   5. render again -> pipe to psql -> APPLY #2 succeeds with NO error (the case that used to roll
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
# then restarts, so we require a real TCP query to succeed (not a unix socket, which stays up
# during the temp-server phase), and we poll generously for a cold init.
ready=""
for _ in $(seq 1 120); do
  if docker exec "$CID" psql -h 127.0.0.1 -U postgres -d postgres -tAc "SELECT 1" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
[ -n "$ready" ] || { echo "ERROR: postgres never became ready" >&2; docker logs "$CID" 2>&1 | tail -20 >&2; exit 2; }

# Pipe SQL into the container's psql with ON_ERROR_STOP; a nonzero exit == a SQL error surfaced.
psql_run() { docker exec -i "$CID" psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -U postgres -d postgres -tA "$@"; }
psql_run_as() { local u="$1"; shift; docker exec -i "$CID" psql -h 127.0.0.1 -v ON_ERROR_STOP=1 -U "$u" -d postgres -tA "$@"; }

# --- 0) base tables + two deploy identities (with and without CREATEROLE), NO app roles yet ------
# Each table gets its app_schema + a base table carrying its PK column and every index column, so
# the rendered grants/indexes/view resolve. Derived from config so it never drifts from tables.json.
echo "--- creating base schemas + tables + two deploy identities from $CONFIG ---"
"$PY" - "$CONFIG" <<'PY' | psql_run
import json, sys
tables = json.load(open(sys.argv[1]))
out = [
    "CREATE ROLE deploy_owner NOLOGIN;",
    "CREATE ROLE deploy_with_createrole LOGIN CREATEROLE IN ROLE deploy_owner;",
    "CREATE ROLE deploy_no_createrole LOGIN NOCREATEROLE IN ROLE deploy_owner;",
]
for t in tables:
    schema = t["app_schema"]
    tbl = t["synced_table_id"].split(".")[-1]
    cols = {t.get("primary_key_columns", ["id"])[0]: "int"}
    for c in t.get("index_columns", []):
        cols.setdefault(c, "text")
    coldefs = ", ".join(f"{name} {typ}" for name, typ in cols.items())
    out.append(f"CREATE SCHEMA IF NOT EXISTS {schema};")
    out.append(f"ALTER SCHEMA {schema} OWNER TO deploy_owner;")
    out.append(f"CREATE TABLE IF NOT EXISTS {schema}.{tbl} ({coldefs});")
    out.append(f"ALTER TABLE {schema}.{tbl} OWNER TO deploy_owner;")
print("\n".join(out))
PY
echo "base tables created (app roles NOT created yet)."

render() { "$PY" -m dabs.render_ddl --config "$CONFIG"; }
( cd "$REPO_ROOT" && render >/tmp/lkb_render_ddl.sql )
echo "--- rendered $(grep -c ';' /tmp/lkb_render_ddl.sql) statements (no alembic_version below) ---"
if grep -qi "alembic_version" /tmp/lkb_render_ddl.sql; then
  echo "ERROR: rendered SQL contains alembic_version — the version-table-free renderer regressed" >&2; exit 1
fi
# Attacker 5: active SQL must never contain CREATE ROLE
if grep -v '^[[:space:]]*--' /tmp/lkb_render_ddl.sql | grep -qiE '\bCREATE[[:space:]]+ROLE\b'; then
  echo "ERROR: rendered SQL contains CREATE ROLE — migration must check the app role, never create it" >&2; exit 1
fi

FIRST_ROLE="$("$PY" -c "import json;t=json.load(open('$CONFIG'))[0];print(t['app_role'])")"
EXPECTED_ERR="app role ${FIRST_ROLE} does not exist; an admin must create it"

# --- Attacker 1: role missing, deploy identity WITH CREATEROLE ---
echo "--- Attacker 1: role missing, deploy identity WITH CREATEROLE ---"
set +e
err_with_cr="$(psql_run_as deploy_with_createrole </tmp/lkb_render_ddl.sql 2>&1)"
rc_with_cr=$?
set -e
if [ "$rc_with_cr" -eq 0 ]; then
  echo "ERROR: Attacker 1 failed — deploy WITH CREATEROLE succeeded when ${FIRST_ROLE} was missing!" >&2; exit 1
fi
if ! printf '%s' "$err_with_cr" | grep -qF "$EXPECTED_ERR"; then
  echo "ERROR: Attacker 1 failed — expected '$EXPECTED_ERR', got:" >&2
  echo "$err_with_cr" >&2; exit 1
fi
role_exists_after="$(psql_run -c "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${FIRST_ROLE}');" | tr -d '[:space:]')"
if [ "$role_exists_after" != "f" ]; then
  echo "ERROR: Attacker 1 failed — pg_roles has ${FIRST_ROLE} after failed deploy!" >&2; exit 1
fi
echo "Attacker 1 OK: failed non-zero with exact message; pg_roles has no ${FIRST_ROLE}."

# --- Attacker 2: role missing, deploy identity WITHOUT CREATEROLE ---
echo "--- Attacker 2: role missing, deploy identity WITHOUT CREATEROLE ---"
set +e
err_no_cr="$(psql_run_as deploy_no_createrole </tmp/lkb_render_ddl.sql 2>&1)"
rc_no_cr=$?
set -e
if [ "$rc_no_cr" -eq 0 ]; then
  echo "ERROR: Attacker 2 failed — deploy WITHOUT CREATEROLE succeeded when ${FIRST_ROLE} was missing!" >&2; exit 1
fi
if ! printf '%s' "$err_no_cr" | grep -qF "$EXPECTED_ERR"; then
  echo "ERROR: Attacker 2 failed — expected '$EXPECTED_ERR', got:" >&2
  echo "$err_no_cr" >&2; exit 1
fi
echo "Attacker 2 OK: failed non-zero with exact message."

# --- Attacker 3: admin pre-creates the app roles (one-time admin step) ---
echo "--- Admin pre-creates app roles from $CONFIG ---"
"$PY" - "$CONFIG" <<'PY' | psql_run
import json, sys
for t in json.load(open(sys.argv[1])):
    print(f"CREATE ROLE {t['app_role']} NOLOGIN;")
PY

echo "--- APPLY #1 (as deploy_no_createrole) ---"
psql_run_as deploy_no_createrole </tmp/lkb_render_ddl.sql
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
psql_run_as deploy_no_createrole </tmp/lkb_render_ddl.sql
echo "apply #2 OK — no rollback."

# --- assert the wiped objects reconciled back AND the app role/grant survive --------------------
# The LAST column is the least-privilege proof: the app role can SELECT the consumer VIEW but must
# NOT hold SELECT on the base synced table (a Postgres view checks the base-table privilege as the
# view OWNER, not the caller, so the role never needs it — and granting it would let the role bypass
# the row-filterable view). So we require the base-table privilege to be 'f'.
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
