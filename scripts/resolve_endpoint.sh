#!/usr/bin/env bash
# Resolve a Lakebase branch's READ_WRITE compute endpoint and mint a runtime OAuth credential
# FROM that endpoint (the projects / Autoscaling API). Sourced by scripts/deploy.sh and
# scripts/branch_test.sh so host resolution and credential minting always agree on the SAME
# endpoint. Mirrors dabs/migration_job.py::_select_endpoint.
#
# Why the READ_WRITE endpoint: a migration WRITES. A branch has one READ_WRITE endpoint; it may
# also list read-only endpoints. We prefer the READ_WRITE endpoint that exposes a connection host,
# and only fall back to the first endpoint with a host -- stricter than a blind JSON[0] pick, so it
# stays correct even if a read-only endpoint is listed first. BOTH the connection host AND the
# credential-minting endpoint name are derived from that one chosen endpoint.
#
# Projects API (no database-instance name anywhere):
#   databricks postgres list-endpoints projects/<proj>/branches/<branch> -o json
#   databricks postgres generate-database-credential <endpoint-name> -o json   -> {"token": ...}
#
# On success sets three exported globals for the caller:
#   LB_HOST      -- the chosen endpoint's connection host (status.hosts.host)
#   LB_ENDPOINT  -- the chosen endpoint's resource name (projects/<p>/branches/<b>/endpoints/<e>)
#   LB_TOKEN     -- a short-lived workspace-scoped OAuth token minted FROM that endpoint
#
# Usage:
#   . "$(dirname "$0")/resolve_endpoint.sh"
#   lakebase_resolve_and_mint "projects/<proj>/branches/<branch>" [profile]

# Read `list-endpoints` JSON on stdin and print "<host><TAB><endpoint-name>" for the READ_WRITE
# endpoint (fallback: the first endpoint that exposes a host). Non-zero if none has a host/name.
# NB: the program is passed via `python3 -c` (not a `python3 - <<HEREDOC`, which would make the
# heredoc python's PROGRAM via `-` and swallow the piped JSON on stdin).
_lakebase_select_endpoint() {
  python3 -c '
import json, sys

eps = json.load(sys.stdin)

def host(ep):
    return ((((ep or {}).get("status") or {}).get("hosts") or {}).get("host")) or ""

def is_read_write(ep):
    etype = ((ep or {}).get("status") or {}).get("endpoint_type")
    return "READ_WRITE" in str(etype or "").upper()

chosen = None
for ep in eps:
    if is_read_write(ep) and host(ep):
        chosen = ep
        break
if chosen is None:
    for ep in eps:
        if host(ep):
            chosen = ep
            break
if chosen is None:
    sys.stderr.write("no compute endpoint with a connection host found for the branch\n")
    sys.exit(1)

name = (chosen.get("name") or "")
if not name:
    sys.stderr.write("resolved compute endpoint has no resource name to mint a credential from\n")
    sys.exit(1)

# host and name from the SAME chosen endpoint; tab-separated (neither field can contain a tab).
sys.stdout.write(host(chosen) + "\t" + name + "\n")
'
}

# lakebase_resolve_and_mint <branch> [profile]
# Resolve the branch's READ_WRITE endpoint (host + name) and mint a runtime OAuth token from it.
# Sets/exports LB_HOST, LB_ENDPOINT, LB_TOKEN.
#
# `profile` selects a ~/.databrickscfg profile (default DEFAULT for a workstation). Pass an EMPTY
# string ("") to omit `-p` entirely -- e.g. in CI, where auth is env-var based (DATABRICKS_HOST +
# DATABRICKS_CLIENT_ID/SECRET) and no config profile exists.
lakebase_resolve_and_mint() {
  local branch="${1:?branch (projects/<proj>/branches/<branch>) required}"
  local profile="${2-DEFAULT}"
  local selected
  local -a pflag=()
  [ -n "$profile" ] && pflag=(-p "$profile")

  # Command substitution under `set -o pipefail` propagates a failure in either stage; declare the
  # local first so the assignment's own exit status (not `local`'s) reaches `set -e`. The
  # ${pflag[@]+"${pflag[@]}"} guard is the set -u-safe empty-array expansion (bash 3.2).
  selected=$(databricks postgres list-endpoints "$branch" ${pflag[@]+"${pflag[@]}"} -o json </dev/null \
    | _lakebase_select_endpoint)

  LB_HOST="${selected%%$'\t'*}"       # everything before the tab = connection host
  LB_ENDPOINT="${selected##*$'\t'}"   # everything after the tab  = endpoint resource name

  # Mint FROM the chosen endpoint name (no instance name); projects/Autoscaling credential.
  LB_TOKEN=$(databricks postgres generate-database-credential "$LB_ENDPOINT" \
    ${pflag[@]+"${pflag[@]}"} -o json </dev/null \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

  export LB_HOST LB_ENDPOINT LB_TOKEN
}

# lakebase_validate_database <db>
# Validate a Postgres database name against ^[A-Za-z_][A-Za-z0-9_$-]{0,62}$ before building a
# JDBC URL or psycopg/psql conninfo string (prevents URL/conninfo injection such as
# db?sslmode=disable or "db sslmode=disable").
lakebase_validate_database() {
  local db="${1-}"
  if [[ "$db" == *$'\n'* ]] || [[ ! "$db" =~ ^[A-Za-z_][A-Za-z0-9_\$-]{0,62}$ ]]; then
    printf 'invalid database name %q: must match ^[A-Za-z_][A-Za-z0-9_$-]{0,62}$\n' "$db" >&2
    return 1
  fi
}

# lakebase_resolve_database [repo_root]
# Resolve the target Postgres logical database name on no-apply paths (branch_test.sh,
# pr-validate.yml) by asking Terraform (`echo var.logical_database | terraform console`) in a
# provider-free scratch copy of terraform/variables.tf + terraform/terraform.tfvars(.json) +
# terraform/*.auto.tfvars(.json). Terraform itself is the single source of truth for its variable
# precedence (from lowest to highest: variables.tf default -> TF_VAR_logical_database ->
# terraform.tfvars -> terraform.tfvars.json -> *.auto.tfvars / *.auto.tfvars.json in lexical order).
# If the terraform binary is unavailable, fails closed on any terraform.tfvars,
# terraform.tfvars.json, or *.auto.tfvars(.json) file ("unsupported var file: <name>; set
# TF_VAR_logical_database"), and otherwise uses ${TF_VAR_logical_database:-databricks_postgres}.
# PGDATABASE never overrides Terraform's variable resolution. Validates the resolved name via
# lakebase_validate_database before printing it.
lakebase_resolve_database() {
  local root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  local tf_dir="$root/terraform"
  local db=""

  if ! db="$(python3 - "$tf_dir" <<'PY'
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

tf_dir = pathlib.Path(sys.argv[1])
vars_tf = tf_dir / "variables.tf"


def ask_terraform_console() -> str | None:
    if shutil.which("terraform") is None or not vars_tf.is_file():
        return None
    with tempfile.TemporaryDirectory(prefix="lb-tfvars-") as tmp:
        scratch = pathlib.Path(tmp)
        shutil.copy(vars_tf, scratch / "variables.tf")
        for p in sorted(tf_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name in ("terraform.tfvars", "terraform.tfvars.json") or p.name.endswith(
                (".auto.tfvars", ".auto.tfvars.json")
            ):
                shutil.copy(p, scratch / p.name)
        res = subprocess.run(
            ["terraform", f"-chdir={scratch}", "console"],
            input="var.logical_database\n",
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return None
        lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
        if not lines:
            return None
        try:
            val = json.loads(lines[-1])
            return val if isinstance(val, str) else None
        except Exception:
            return None


val = ask_terraform_console()
if val is None:
    if tf_dir.is_dir():
        for p in sorted(tf_dir.iterdir()):
            if p.is_file() and (
                p.name in ("terraform.tfvars", "terraform.tfvars.json")
                or p.name.endswith((".auto.tfvars", ".auto.tfvars.json"))
            ):
                sys.stderr.write(f"unsupported var file: {p.name}; set TF_VAR_logical_database\n")
                sys.exit(1)
    val = os.environ.get("TF_VAR_logical_database", "databricks_postgres")

sys.stdout.write(val)
PY
)"; then
    return 1
  fi

  lakebase_validate_database "$db" || return 1
  printf '%s\n' "$db"
}

