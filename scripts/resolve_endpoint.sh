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
  selected=$(databricks postgres list-endpoints "$branch" ${pflag[@]+"${pflag[@]}"} -o json \
    | _lakebase_select_endpoint)

  LB_HOST="${selected%%$'\t'*}"       # everything before the tab = connection host
  LB_ENDPOINT="${selected##*$'\t'}"   # everything after the tab  = endpoint resource name

  # Mint FROM the chosen endpoint name (no instance name); projects/Autoscaling credential.
  LB_TOKEN=$(databricks postgres generate-database-credential "$LB_ENDPOINT" \
    ${pflag[@]+"${pflag[@]}"} -o json \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

  export LB_HOST LB_ENDPOINT LB_TOKEN
}
