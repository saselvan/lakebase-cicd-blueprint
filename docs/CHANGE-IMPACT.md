# Change impact: every grant, role and ownership step

Before you change one of these steps, find its row. Each row says what must be true first, what breaks
downstream if it is wrong, and what proves it. Evidence kinds:

- **Offline**: a test in the no-database suite (`pytest`).
- **Docker**: a script against a real PostgreSQL 16 in a throwaway container.
- **Live**: a dated run on a Lakebase Autoscaling child branch, `1-8 CU`, synthetic data, deleted afterwards.
- **Docs**: stated in the docs only, not run in this repo. A row with only this kind says so.

Last checked: 2026-10-29, against the code on `main` plus the before-apply deploy checks.

## The steps

| # | Step | Who runs it | Must be true first | Breaks if wrong | Offline / Docker | Live |
|---|---|---|---|---|---|---|
| 1 | App role exists (`CREATE ROLE <app_role> NOLOGIN`, once) | An admin: the pipeline never creates roles | Admin has `CREATEROLE` or owns the project | `migrate` stops with `app role <name> does not exist; an admin must create it` and changes nothing; `verify` fails `role ... exists` | `test_render_ddl.py::test_role_guard_sql_validates_identifier_and_emits_raise_exception`; `docker_double_apply_proof.sh` (missing role, with and without `CREATEROLE`) | 2026-10-29: `migrate` with a missing role exited 1 with that message; no view and no role were left behind. 2026-10-09: `CREATE ROLE` by a non-`CREATEROLE` service principal fails |
| 2 | `GRANT USAGE ON SCHEMA <app_schema> TO <app_role>` | Deploy identity, every deploy | The schema exists; the deploy identity may grant on it | The app role cannot resolve the view | `test_render_ddl.py::test_grant_statements_grants_usage_only_no_base_table_select`; `test_verify_step.py::test_missing_schema_usage_exits_nonzero`; `docker_verify_step.sh` | 2026-10-09: the non-superuser deploy service principal ran the grant. 2026-10-15, 2026-10-19, 2026-10-23, 2026-10-29: `verify` printed `PASS role ... has USAGE on schema` |
| 3 | `GRANT SELECT ON <schema>.<view> TO <app_role>` | Deploy identity, every deploy | The view exists and the deploy identity owns it; the app role exists | The app reads nothing; `verify` fails `can SELECT` and `a real read` | `test_render_ddl.py::test_app_role_only_table_level_select_is_the_view`; `test_verify_step.py::test_missing_grant_on_view_exits_nonzero`; `docker_verify_step.sh` and `docker_double_apply_proof.sh` (revoke, then heal) | 2026-10-14 and 2026-10-23: a `REVOKE` by the grantor made `verify` exit 1 and re-applying the rendered SQL healed it (`VERIFY OK`); a `REVOKE` by the project owner of a grant the deploy service principal made changed nothing |
| 4 | The app role has **no** route to the base synced table: not direct, column, `PUBLIC`, inherited, `NOINHERIT`, or `DELETE`/`TRUNCATE`/`TRIGGER` | Nobody grants it. `migrate` never emits it; `verify` checks all routes | No stray grant on the table. A branch copied from a database that had one inherits it | The app role can read the table and bypasses the view | `test_checks.py::test_verify_fails_when_the_app_role_can_reach_the_synced_table`; `liquibase/tests/test_generate_changelogs.py::test_only_view_select_is_granted_across_changelog`; `docker_*` | 2026-10-19: all 6 routes failed `verify`. 2026-10-23: a stray direct `SELECT` inherited from a copied branch was caught, then removed with `REVOKE` |
| 5 | The deploy identity creates and **owns** the consumer view (`DROP VIEW IF EXISTS`, `CREATE OR REPLACE VIEW`) | Deploy identity, every deploy | The deploy identity can read the synced table (it created it and holds `SELECT ... WITH GRANT OPTION`) | A view owned by someone else cannot be dropped or replaced: the project owner gets `must be owner of view` | `test_render_ddl.py::test_view_statements_drops_before_create_and_regrants`; `docker_view_column_change_proof.sh` | 2026-10-14: the project owner could not drop a service-principal-owned view or schema. 2026-10-15: the deploy service principal dropped and recreated it |
| 6 | The consumer view has no `security_invoker` option | `migrate` creates it plain; `verify` checks `pg_class.reloptions` | The view was created by this pipeline | With `security_invoker = true`, PostgreSQL checks the base table as the app role, which is denied; the app breaks while the deploy identity's own read still works | `test_checks.py::test_verify_fails_when_consumer_view_has_security_invoker_enabled`; `test_verify_step.py::test_security_invoker_enabled_exits_nonzero`; `docker_verify_step.sh` | 2026-10-29: `ALTER VIEW ... SET (security_invoker = true)` made `verify` exit 1 with only that check failing; `RESET` returned it to `VERIFY OK` |
| 7 | Identity policy guard (prod target): the job runs as the expected service principal, which is not in `databricks_superuser`, `pg_read_all_data` or `pg_write_all_data`, directly or through any chain of roles | The `verify_identity_policy` task, before `migrate` | `--expected-identity` is set to the deploy service principal | A job running as a wide-rights identity changes the database with more rights than the pattern assumes | `test_checks.py::test_policy_fails_on_every_route_to_wide_rights`; `test_policy_fails_when_the_job_runs_as_another_identity` | 2026-10-14 to 2026-10-23 (README "Tested vs not tested"): the guard passed for the deploy service principal and failed on `databricks_superuser` |
| 8 | Grants, indexes and view are reapplied on every deploy (`runAlways`) | Deploy identity, every deploy | The synced table is `ONLINE` | A synced-table replace drops its grants; without the reapply the app reads nothing | `docker_double_apply_proof.sh` (second apply is a clean no-op); `test_deploy_drift_gate.py` | 2026-10-15: a primary-key replace dropped the grants; `migrate` put them back and `verify` passed |
| 9 | A direct `GRANT` on the synced table by an identity other than the creator | Not a pipeline step: what the pattern avoids | n/a | `WARNING: no privileges were granted`; the app stays denied. The pipeline reads through the view instead | Offline: no statement in the renderer or the changelogs grants on the table (`test_least_privilege_parser_catches_all_five_grant_select_spellings`) | 2026-10-14: a non-creator got the warning; the creator's direct grant worked |
| 10 | `GRANT ... ON ALL TABLES IN SCHEMA` and `ALTER DEFAULT PRIVILEGES` | Not used. The pipeline grants named objects only | n/a | `ALTER DEFAULT PRIVILEGES` run by the project owner does not cover synced tables, which a `databricks_writer_<n>` role owns | Offline: the least-privilege parser (five spellings) rejects them in the changelogs | 2026-10-21: default privileges by the owner left `has_table_privilege` false on a synced table |
| 11 | Rotating the deploy service principal | An admin, rarely | A plan for downtime of the rebuild | `REASSIGN OWNED` alone is not enough: the new principal gets `permission denied for synced table operation` on `CREATE INDEX` | Docs: `RUNBOOK.md` "Rotating the deploy service principal" | 2026-10-14: `REASSIGN OWNED` alone failed; `REASSIGN OWNED`, then the new principal drops the view, the old synced table is deleted and the deploy runs as the new principal worked |

## What no test here covers

- **A second person or identity running the README from zero.** Every live run above used the author's workspace
  login. On 2026-10-29 an independent agent (no project context) ran the whole DABs path from zero with that
  login and passed; that tests the README, not another person's permissions. (Also listed in the README under
  "Tested vs not tested".)
- **Per-consumer-group `WHERE` views** and **row-level security on a synced table.** Postgres refuses `ENABLE ROW
  LEVEL SECURITY` on a table you do not own; the per-group view pattern is described in `docs/DESIGN-NOTES.md` as
  not tested.
- **A sync-mode change with a dependent view.** It can wedge the table; see the README's self-healing caveat.
- **Another Postgres version.** Live runs used PostgreSQL 16.

## When you change a step

1. Find its row. Check the "Breaks if wrong" cell against your change.
2. Change the renderer (`dabs/render_ddl.py`) once. The DABs job and the Liquibase changelogs both come from it;
   regenerate the changelogs and run both drift checks.
3. Add or update the test named in the row. A step with no test in its row is not finished.
4. If the behaviour is about Lakebase and not about this code, run it on a throwaway child branch and add a dated
   entry to the "Live" cell. Do not date a claim you did not run.
