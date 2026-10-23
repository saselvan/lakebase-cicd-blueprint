#!/usr/bin/env bash
# scripts/verify_table.sh -- step 4 of the pipeline, factored out so it is INDEPENDENTLY testable.
#
# The bug this fixes: scripts/deploy.sh step 4 used to run its psql checks as
# `psql ... 2>&1 || true` and NEVER inspect the output. So `has_table_privilege(...)` could return
# 'f' (or an index could be missing) and the pipeline still printed success and exited 0. The
# README/RUNBOOK sell step 4 as THE guarantee that the app role can read and the indexes exist, but
# as written it could not fail.
#
# verify_table now ASSERTS the real post-conditions and RETURNS NON-ZERO if any is false. deploy.sh
# sources this file and calls verify_table per table under `set -e`, so the first table that fails
# aborts the pipeline with a non-zero exit instead of being swallowed.
#
# It reads the database connection from the standard PG* env vars (PGHOST/PGPORT/PGDATABASE/PGUSER/
# PGPASSWORD/PGSSLMODE), exactly as scripts/deploy.sh exports them before the per-table loop.
#
# Sourced by scripts/deploy.sh and .github/workflows/pr-validate.yml; also runnable standalone:
#   scripts/verify_table.sh ROLE SCHEMA PG_TABLE VIEW [INDEX_COL ...]
#   scripts/verify_table.sh --from-row ROW_JSON

# Capture the repo root at source/load time so callers that later `cd liquibase` (such as
# .github/workflows/pr-validate.yml after sourcing `. scripts/verify_table.sh` relatively) still
# resolve dabs/checks.py and dabs/render_ddl.py against the repo root.
_VERIFY_TABLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# _scalar QUERY -- run a single-cell psql query and print the trimmed scalar. On any psql error the
# result is the empty string, which FAILS the assertion below instead of passing it: the whole point
# of the fix is that the result is inspected, never `|| true`-swallowed.
#
# Surface the cause on failure: capture psql's stderr and, ONLY when psql exits non-zero, surface it on our stderr so a
# FAIL line carries the CAUSE (connection refused, missing relation, auth error), not just an empty
# value. The happy path stays quiet: when psql succeeds we print nothing extra, even if it wrote a
# NOTICE to stderr. stderr goes to a temp file so the scalar captured by the caller (via command
# substitution, which grabs stdout only) is never polluted by the diagnostic.
_scalar() {
  local out err rc errfile
  errfile="$(mktemp "${TMPDIR:-/tmp}/verify_scalar.XXXXXX")"
  out="$(psql -tAqc "$1" 2>"$errfile")"
  rc=$?
  err="$(cat "$errfile")"
  rm -f "$errfile"
  if [ "$rc" -ne 0 ]; then
    out=""
    if [ -n "$err" ]; then
      printf 'psql error: %s\n' "$err" >&2
    fi
  fi
  printf '%s' "$out" | tr -d '[:space:]'
}

# _expect_t LABEL QUERY -- PASS iff the scalar query returns exactly 't'. Prints one PASS/FAIL line
# and returns non-zero on FAIL. Callers count failures; nothing is swallowed.
_expect_t() {
  local label="$1" got
  got="$(_scalar "$2")"
  if [ "$got" = "t" ]; then
    echo "  PASS $label"
    return 0
  fi
  echo "  FAIL $label (query returned '${got:-<empty/error>}', expected 't')"
  return 1
}

# verify_table ROLE SCHEMA PG_TABLE VIEW [INDEX_COL ...]
# Asserts the post-conditions README/RUNBOOK step 4 promises, and RETURNS NON-ZERO if any fails:
#   (c) the app role exists, the consumer view exists, and the view does not have security_invoker
#       enabled in pg_class.reloptions,
#   (a) the app role can SELECT the consumer VIEW (the no-superuser read path),
#   (d) the app role has USAGE on the schema, and a real read through the view succeeds (as the
#       deploy identity, which owns the view),
#   (e) the app role is DENIED on the base synced table by every route (table, column, PUBLIC,
#       inherited, and NOINHERIT role grants, for read and write privileges alike), and
#   (b) every expected index idx_<pg_table>_<col> exists.
# Both DABs verify and this script share one check list from dabs/checks.py (--table-checks).
# If the CLI fails or prints 0 checks, verify_table fails closed (returns 1).
verify_table() {
  if [ "$#" -lt 4 ]; then
    echo "verify_table: usage: verify_table ROLE SCHEMA PG_TABLE VIEW [INDEX_COL ...]" >&2
    return 2
  fi
  local role="$1" schema="$2" pg_table="$3" view="$4"
  shift 4
  local failures=0 count=0 root checks_out label sql

  echo "== 4. verify table '$pg_table' (role=$role, view=$schema.$view) =="

  root="${_VERIFY_TABLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  if ! checks_out="$(python3 "$root/dabs/checks.py" --table-checks "$role" "$schema" "$pg_table" "$view" "$@")"; then
    echo "  VERIFY FAILED for '$pg_table': dabs/checks.py --table-checks exited non-zero" >&2
    return 1
  fi
  if [ -z "$checks_out" ]; then
    echo "  VERIFY FAILED for '$pg_table': dabs/checks.py --table-checks emitted 0 checks" >&2
    return 1
  fi

  while IFS=$'\t' read -r label sql <&3; do
    [ -z "$label" ] && continue
    count=$((count + 1))
    _expect_t "$label" "$sql" || failures=$((failures + 1))
  done 3<<< "$checks_out"

  if [ "$count" -eq 0 ]; then
    echo "  VERIFY FAILED for '$pg_table': dabs/checks.py --table-checks emitted 0 checks" >&2
    return 1
  fi

  if [ "$failures" -ne 0 ]; then
    echo "  VERIFY FAILED for '$pg_table': $failures post-condition(s) not met" >&2
    return 1
  fi
  echo "  VERIFY OK for '$pg_table'"
  return 0
}

# verify_table_from_row ROW_JSON
# Parse one config/tables.json object (compact JSON row) into ROLE, SCHEMA, PG_TABLE, VIEW
# (resolved through the shared seam dabs.render_ddl.resolve_view_name), and INDEX_COLUMNS, then
# invoke verify_table with the exact same positional arguments as scripts/deploy.sh.
verify_table_from_row() {
  if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
    echo "verify_table_from_row: usage: verify_table_from_row ROW_JSON" >&2
    return 2
  fi
  local row="$1" root eval_out
  local T_ROLE="" T_SCHEMA="" PG_TABLE="" T_VIEW=""
  local -a T_IDX=()
  root="${_VERIFY_TABLE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  if ! eval_out="$(python3 - "$row" "$root" <<'PY'
import json, shlex, sys
sys.path.insert(0, sys.argv[2])
from dabs.render_ddl import resolve_view_name
t = json.loads(sys.argv[1])
def emit(k, v): print(f"{k}={shlex.quote(str(v))}")
pg_table = str(t["synced_table_id"]).split(".")[-1]
emit("T_ROLE",   t["app_role"])
emit("T_SCHEMA", t["app_schema"])
emit("PG_TABLE", pg_table)
emit("T_VIEW",   resolve_view_name(t, pg_table))
cols = t.get("index_columns") or []
print("T_IDX=(" + " ".join(shlex.quote(str(c)) for c in cols) + ")")
PY
)"; then
    echo "  VERIFY FAILED: could not parse table config row" >&2
    return 1
  fi
  eval "$eval_out"
  if [ "${#T_IDX[@]}" -gt 0 ]; then
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "$T_VIEW" "${T_IDX[@]}"
  else
    verify_table "$T_ROLE" "$T_SCHEMA" "$PG_TABLE" "$T_VIEW"
  fi
}

# Standalone execution (not sourced): verify one table from CLI args. deploy.sh sources this file
# (this guard is false there), so the function is defined without running.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -euo pipefail
  if [ "${1:-}" = "--from-row" ]; then
    shift
    verify_table_from_row "$@"
  else
    verify_table "$@"
  fi
fi
