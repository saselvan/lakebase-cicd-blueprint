#!/usr/bin/env python3
"""
Illustrative consumer: read a synced table THROUGH its consumer view (ADR 0001).

An app reads the view (e.g. `cicd_proj.members_v`) that the deploy identity created and granted
to the app role — never the writer-owned base table, and never as a superuser.

This is a minimal example. For production, use a connection POOL and let the OAuth token refresh
per connection (tokens are short-lived, ~1h). See the Databricks tutorial:
https://docs.databricks.com/aws/en/oltp/projects/tutorial-databricks-apps-autoscaling

Credentials come from the projects (Autoscaling) API: given a branch
(`projects/<project>/branches/<branch>`), resolve its READ_WRITE compute endpoint, then derive
BOTH the connection host AND the credential-minting endpoint name from that SAME endpoint. There
is no database-instance name. This mirrors scripts/resolve_endpoint.sh and
dabs/migration_job.py::_select_endpoint.

Requirements: psycopg[binary] (psycopg3), Databricks CLI authenticated.
Env: BRANCH (projects/<project>/branches/<branch>), PGUSER (your Databricks username),
     PROFILE (CLI profile, default DEFAULT), VIEW (schema.view, default cicd_proj.members_v).
     PGHOST is optional — if unset it is derived from the resolved endpoint.
"""
import json
import os
import subprocess

import psycopg  # psycopg3


def _cli_json(args: list[str], profile: str) -> object:
    """Run a `databricks` CLI command with `-o json` and return the parsed payload."""
    cmd = ["databricks", *args]
    if profile:
        cmd += ["-p", profile]
    cmd += ["-o", "json"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def resolve_endpoint(branch: str, profile: str) -> tuple[str, str]:
    """Return (host, endpoint_name) for a branch's READ_WRITE compute endpoint.

    A branch may list read-only endpoints too; we prefer the READ_WRITE endpoint that exposes a
    connection host, and only fall back to the first endpoint with a host. Host and name come from
    the SAME chosen endpoint.
    """
    endpoints = _cli_json(["postgres", "list-endpoints", branch], profile)

    def host(ep: dict) -> str:
        return ((ep.get("status") or {}).get("hosts") or {}).get("host") or ""

    def is_read_write(ep: dict) -> bool:
        etype = (ep.get("status") or {}).get("endpoint_type")
        return "READ_WRITE" in str(etype or "").upper()

    chosen = next((ep for ep in endpoints if is_read_write(ep) and host(ep)), None)
    if chosen is None:
        chosen = next((ep for ep in endpoints if host(ep)), None)
    if chosen is None:
        raise RuntimeError("no compute endpoint with a connection host found for the branch")
    name = chosen.get("name") or ""
    if not name:
        raise RuntimeError("resolved compute endpoint has no resource name to mint a credential from")
    return host(chosen), name


def mint_token(endpoint: str, profile: str) -> str:
    """Short-lived Postgres OAuth token — minted at runtime FROM a branch endpoint, never stored."""
    return _cli_json(["postgres", "generate-database-credential", endpoint], profile)["token"]


def main() -> None:
    profile = os.environ.get("PROFILE", "DEFAULT")
    branch = os.environ["BRANCH"]
    user = os.environ["PGUSER"]
    view = os.environ.get("VIEW", "cicd_proj.members_v")

    host, endpoint = resolve_endpoint(branch, profile)
    host = os.environ.get("PGHOST", host)  # allow an explicit override; otherwise use the endpoint's
    token = mint_token(endpoint, profile)

    # In production use psycopg_pool.ConnectionPool with a per-connection token callback.
    with psycopg.connect(
        host=host, port=5432, dbname="databricks_postgres",
        user=user, password=token, sslmode="require",
    ) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {view}")
        print(f"{view}: {cur.fetchone()[0]} rows readable via the view")


if __name__ == "__main__":
    main()
