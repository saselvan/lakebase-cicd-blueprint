#!/usr/bin/env bash
# Offline `databricks bundle validate` gate for the DABs variant — NO cloud, NO secrets.
#
# The seam this proves: config/tables.json --(codegen)--> dabs/resources/*.yml --(validate)--> exit code.
# It regenerates the bundle resources into an ISOLATED temp bundle (never the committed tree) for
# validation, so a missing/broken codegen step surfaces as absent resources here. It ALSO verifies
# the COMMITTED dabs/resources/*.yml byte-match a fresh generation (drift check) — otherwise the
# committed files could silently diverge from config/tables.json and this gate would never notice,
# because validation only ever ran the freshly generated copy. Validation runs in --strict mode
# (warnings are errors), against a localhost stub that answers the single SCIM `Me` / get-status
# call the CLI makes — no real workspace needed.
#
# Falsifiability (the reviewer's mutations):
#   1. Corrupt config/tables.json (invalid JSON)  -> codegen step fails       -> this script exits non-zero.
#   2. Codegen emits an invalid resource field    -> `bundle validate --strict` errors -> non-zero.
#   3. Remove the temp codegen step below         -> temp/resources is empty  -> the count check exits non-zero.
#   4. Edit/delete a COMMITTED dabs/resources/*.yml -> the drift check (step 0) exits non-zero.
#
# Requires: databricks CLI >= 1.5.0 (Lakebase DAB resources are Beta — older CLIs drop role_id).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG="${CONFIG:-$REPO_ROOT/config/tables.json}"

# --- CLI version gate (>= 1.5.0) --------------------------------------------------------------
ver="$(databricks version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [ -z "$ver" ]; then echo "ERROR: databricks CLI not found on PATH" >&2; exit 2; fi
min="1.5.0"
if [ "$(printf '%s\n%s\n' "$min" "$ver" | sort -V | head -1)" != "$min" ]; then
  echo "ERROR: databricks CLI $ver < required $min (Lakebase DABs Beta needs >= $min)" >&2; exit 2
fi
echo "databricks CLI $ver (>= $min) OK"

# --- Isolated temp bundle ---------------------------------------------------------------------
WORK="$(mktemp -d)"
MOCK_LOG="$(mktemp)"
MOCK_PID=""
cleanup() { [ -n "$MOCK_PID" ] && kill "$MOCK_PID" 2>/dev/null || true; rm -rf "$WORK" "$MOCK_LOG"; }
trap cleanup EXIT

cp "$REPO_ROOT/dabs/databricks.yml" "$WORK/databricks.yml"

# --- 0) Drift check: the COMMITTED dabs/resources/*.yml must byte-match a fresh generation. ----
# Verify-only (writes nothing). If someone edits tables.json without re-running codegen, or hand-
# edits a committed resource, this fails BEFORE we validate — the committed tree is what ships.
echo "--- drift check: committed dabs/resources vs fresh codegen from config ---"
( cd "$REPO_ROOT" && python3 -m dabs.generate_resources --check --config "$CONFIG" )

# --- 1) Codegen into an ISOLATED temp dir for validation. Mutation 3 removes this line. --------
python3 -m dabs.generate_resources --config "$CONFIG" --out "$WORK/resources"

# --- 2) Localhost stub for offline auth (answers the CLI's Me / get-status calls) -------------
python3 -m dabs.tests.mock_workspace --port 0 >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
disown "$MOCK_PID" 2>/dev/null || true  # suppress the shell's "Terminated" notice on cleanup
for _ in $(seq 1 50); do grep -q '^PORT=' "$MOCK_LOG" && break; sleep 0.1; done
PORT="$(sed -n 's/^PORT=//p' "$MOCK_LOG" | head -1)"
if [ -z "$PORT" ]; then echo "ERROR: mock workspace did not start" >&2; cat "$MOCK_LOG" >&2; exit 2; fi
export DATABRICKS_HOST="http://127.0.0.1:$PORT"
export DATABRICKS_TOKEN="offline-dummy-not-a-secret"
unset DATABRICKS_CONFIG_PROFILE DATABRICKS_CONFIG_FILE 2>/dev/null || true

# --- 3) Validate both targets in strict mode -------------------------------------------------
for target in dev prod; do
  echo "--- bundle validate --target $target --strict ---"
  ( cd "$WORK" && databricks bundle validate --target "$target" --strict )
done

# --- 4) Count check: the bundle must carry one synced table + one role per config row ---------
expected="$(python3 -c "import json,sys; print(len(json.load(open('$CONFIG'))))")"
json="$(cd "$WORK" && databricks bundle validate --target dev -o json 2>/dev/null)"
read -r n_synced n_roles < <(python3 - "$expected" <<PY
import json,sys
d=json.loads('''$json''')
r=d.get("resources",{}) or {}
print(len(r.get("postgres_synced_tables") or {}), len(r.get("postgres_roles") or {}))
PY
)
echo "resources: synced=$n_synced roles=$n_roles expected=$expected"
if [ "$n_synced" != "$expected" ] || [ "$n_roles" != "$expected" ]; then
  echo "ERROR: expected $expected synced tables and $expected roles; got synced=$n_synced roles=$n_roles" >&2
  echo "       (did the codegen step run? absent/stale resources fail here by design.)" >&2
  exit 1
fi

echo "OFFLINE VALIDATE OK: both targets strict-validated; $expected table(s) fully generated."
