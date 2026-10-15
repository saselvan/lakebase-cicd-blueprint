"""Post-migration checks for the DABs job: `verify` (every target) and the identity policy guard
(`verify_identity_policy`, prod target only).

Each check is a (label, SQL) pair whose SQL returns ONE boolean. `run_checks` runs every check,
prints one PASS/FAIL line per check, and returns the number of failures. A check passes only when
its query returns exactly True: False, no row, NULL, and an error all count as a FAIL, so a broken
check can never pass quietly.

verify, per configured table:
  * the app role exists and the consumer view exists;
  * the app role can SELECT the view and has USAGE on the schema;
  * a real read through the view succeeds (as the deploy identity, which owns the view);
  * the app role is DENIED on the base synced table by every route: table, column, PUBLIC and
    inherited grants, for read and write privileges alike;
  * each configured index exists.

The identity policy guard runs BEFORE migrate in the prod target, so the job changes nothing in
Postgres under an identity that fails policy. It fails unless:
  * the job runs as the expected deploy identity (current_user), and
  * that identity is not a member of databricks_superuser, pg_read_all_data or pg_write_all_data,
    directly or through any chain of roles (pg_has_role 'MEMBER' follows the chain, including
    NOINHERIT links the identity could SET ROLE through), and
  * neither that identity nor any role it belongs to has CREATEROLE or SUPERUSER.
It is a policy check, not a platform rule: a team that chose databricks_superuser for its deploy
identity drops the task from the prod target. It reads only Postgres rights, so it protects the job
steps, not `bundle deploy`.

stdlib-only, like render_ddl, so the serverless task imports it with nothing extra.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable

try:  # normal import (package on sys.path)
    from dabs import render_ddl
except Exception:  # serverless task: migration_job loads this file by path, so load its sibling too
    import importlib.util
    from pathlib import Path

    _spec = importlib.util.spec_from_file_location(
        "dabs_render_ddl", Path(__file__).resolve().parent / "render_ddl.py"
    )
    render_ddl = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(render_ddl)

Check = tuple[str, str]

# Roles whose membership gives read-all, write-all or near-superuser rights on Lakebase.
WIDE_ROLES = ("databricks_superuser", "pg_read_all_data", "pg_write_all_data")

# An application id (UUID) or a user name/email. Anything else is refused, so the expected identity
# can be embedded as a SQL literal safely.
_IDENTITY = re.compile(r"^[A-Za-z0-9@._+-]{1,128}$")


def run_checks(checks: Iterable[Check], execute: Callable[[str], object],
               out: Callable[[str], None] = print) -> int:
    """Run every check (never stopping at the first failure) and return the failure count."""
    failures = 0
    for label, sql in checks:
        try:
            got = execute(sql)
        except Exception as exc:  # the cause belongs on the FAIL line
            out(f"  FAIL {label} (error: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc!r})")
            failures += 1
            continue
        if got is True:
            out(f"  PASS {label}")
        else:
            out(f"  FAIL {label} (query returned {got!r}, expected True)")
            failures += 1
    return failures


def _table_checks(table: dict) -> list[Check]:
    """The verify checks for one configured table."""
    role = render_ddl.validate_identifier(table["app_role"], "app_role")
    schema = render_ddl.validate_identifier(table["app_schema"], "app_schema")
    tbl = render_ddl.validate_identifier(render_ddl.pg_table_name(table), "synced_table")
    view = render_ddl.resolve_view_name(table, tbl)
    cols = [render_ddl.validate_identifier(c, "index_column") for c in (table.get("index_columns") or [])]
    base = f"{schema}.{tbl}"
    out: list[Check] = [
        (f"role {role} exists", f"SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = '{role}')"),
        (f"consumer view {schema}.{view} exists",
         f"SELECT EXISTS(SELECT 1 FROM pg_views WHERE schemaname = '{schema}' AND viewname = '{view}')"),
        (f"role {role} can SELECT {schema}.{view}",
         f"SELECT has_table_privilege('{role}', '{schema}.{view}', 'SELECT')"),
        (f"role {role} has USAGE on schema {schema}",
         f"SELECT has_schema_privilege('{role}', '{schema}', 'USAGE')"),
        (f"a real read through {schema}.{view} succeeds",
         f"SELECT count(*) >= 0 FROM (SELECT 1 FROM {schema}.{view} LIMIT 1) AS read_check"),
        # Column-level grants are invisible to has_table_privilege; has_any_column_privilege sees
        # table and column grants. DELETE/TRUNCATE/TRIGGER exist only at table level. Both functions
        # count PUBLIC and inherited grants.
        (f"role {role} is denied on synced table {base}",
         f"SELECT NOT has_any_column_privilege('{role}', '{base}', 'SELECT, INSERT, UPDATE, REFERENCES')"
         f" AND NOT has_table_privilege('{role}', '{base}', 'DELETE, TRUNCATE, TRIGGER')"),
    ]
    for col in cols:
        idx = f"idx_{tbl}_{col}"
        out.append((f"index {idx} exists",
                    f"SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE schemaname = '{schema}' "
                    f"AND indexname = '{idx}')"))
    return out


def verify_checks(tables: list[dict]) -> list[Check]:
    """Every verify check for every configured table, in config order."""
    render_ddl.validate_view_names_unique(tables)
    return [c for t in tables for c in _table_checks(t)]


def _validate_expected_identity(expected: str) -> str:
    value = (expected or "").strip()
    if not value or "PLACEHOLDER" in value.upper() or value != expected or not _IDENTITY.match(value):
        raise ValueError(
            f"--expected-identity {expected!r} is not a usable deploy identity: pass the deploy "
            "service principal's application id (the prod target sets it from ${var.run_as_sp})"
        )
    return value


def policy_checks(expected_identity: str) -> list[Check]:
    """The identity policy guard's checks. Raises ValueError on an unusable expected identity, so
    the task fails closed before it connects to anything."""
    expected = _validate_expected_identity(expected_identity)
    out: list[Check] = [
        (f"the job runs as the expected deploy identity {expected}",
         f"SELECT current_user = '{expected}'"),
    ]
    for wide in WIDE_ROLES:
        # Look the role up by oid so a Postgres without it reads as "not a member", not an error.
        out.append((f"current_user is not a member of {wide} (directly or through another role)",
                    f"SELECT NOT EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname = '{wide}' "
                    f"AND pg_has_role(current_user, r.oid, 'MEMBER'))"))
    for attr, word in (("rolcreaterole", "CREATEROLE"), ("rolsuper", "SUPERUSER")):
        # pg_has_role(current_user, own oid) is true, so this covers the identity itself too.
        out.append((f"neither current_user nor a role it belongs to has {word}",
                    f"SELECT NOT EXISTS(SELECT 1 FROM pg_roles r WHERE r.{attr} "
                    f"AND pg_has_role(current_user, r.oid, 'MEMBER'))"))
    return out
