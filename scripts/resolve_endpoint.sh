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
  if [[ ! "$db" =~ ^[A-Za-z_][A-Za-z0-9_\$-]{0,62}$ ]]; then
    printf 'invalid database name %q: must match ^[A-Za-z_][A-Za-z0-9_$-]{0,62}$\n' "$db" >&2
    return 1
  fi
}

# lakebase_resolve_database [repo_root]
# Resolve the target Postgres logical database name in this exact order only:
#   1. TF_VAR_logical_database environment variable
#   2. terraform/terraform.tfvars (or terraform output -raw logical_database when state exists)
#   3. terraform/variables.tf default (databricks_postgres)
# PGDATABASE does NOT override terraform.tfvars or variables.tf. Validates the resolved name via
# lakebase_validate_database before printing it.
lakebase_resolve_database() {
  local root="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  local tf_dir="$root/terraform"
  local db=""

  if [ -n "${TF_VAR_logical_database+x}" ]; then
    db="$TF_VAR_logical_database"
  else
    db="$(python3 - "$tf_dir" <<'PY'
import ast
import pathlib
import subprocess
import sys
import tokenize

tf_dir = pathlib.Path(sys.argv[1])
tfvars = tf_dir / "terraform.tfvars"
vars_tf = tf_dir / "variables.tf"
tfstate = tf_dir / "terraform.tfstate"


def read_tfvars_key(path: pathlib.Path, target: str) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        tokens = list(tokenize.tokenize(fh.readline))
    for idx, tok in enumerate(tokens):
        if tok.type == tokenize.NAME and tok.string == target:
            rest = [t for t in tokens[idx + 1 : idx + 6] if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
            if len(rest) >= 2 and rest[0].string == "=" and rest[1].type == tokenize.STRING:
                return str(ast.literal_eval(rest[1].string))
    return None


def read_variables_tf_default(path: pathlib.Path, target: str) -> str | None:
    if not path.is_file():
        return None
    with path.open("rb") as fh:
        tokens = list(tokenize.tokenize(fh.readline))
    in_var = False
    depth = 0
    for idx, tok in enumerate(tokens):
        if not in_var and tok.type == tokenize.NAME and tok.string == "variable":
            rest = [t for t in tokens[idx + 1 : idx + 5] if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT)]
            if rest and rest[0].type == tokenize.STRING and ast.literal_eval(rest[0].string) == target:
                in_var = True
                depth = 0
        elif in_var:
            if tok.string == "{":
                depth += 1
            elif tok.string == "}":
                depth -= 1
                if depth <= 0:
                    in_var = False
            elif depth == 1 and tok.type == tokenize.NAME and tok.string == "default":
                rest = [t for t in tokens[idx + 1 : idx + 5] if t.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.COMMENT)]
                if len(rest) >= 2 and rest[0].string == "=" and rest[1].type == tokenize.STRING:
                    return str(ast.literal_eval(rest[1].string))
    return None


val = read_tfvars_key(tfvars, "logical_database")
if val is None and tfstate.is_file():
    try:
        res = subprocess.run(
            ["terraform", f"-chdir={tf_dir}", "output", "-raw", "logical_database"],
            capture_output=True,
            text=True,
            check=True,
        )
        if res.stdout.strip():
            val = res.stdout.strip()
    except Exception:
        pass
if val is None:
    val = read_variables_tf_default(vars_tf, "logical_database") or "databricks_postgres"
sys.stdout.write(val)
PY
)"
  fi

  lakebase_validate_database "$db" || return 1
  printf '%s\n' "$db"
}

