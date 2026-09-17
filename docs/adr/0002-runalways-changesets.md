# ADR 0002 — Grant/index/view changesets are `runAlways:true`

**Status:** Accepted

## Context
Changing a synced table's sync mode forces a full replacement (destroy + recreate — verified across
all six directed mode transitions). A replacement drops the table's custom indexes and app-role
grants down to the primary key. A normal Liquibase changeset is tracked once in `DATABASECHANGELOG`;
with `runOnChange:true`, a redeploy after a replace sees unchanged checksums, skips, and leaves the
app without access.

## Decision
Mark the grant (`002`), index (`003`), and view (`004`) changesets **`runAlways:true`**. Their SQL is
idempotent — `GRANT` (no-op if held), `CREATE INDEX IF NOT EXISTS`, `CREATE OR REPLACE VIEW` — so
re-running every deploy is safe, and it rebuilds access after a table replacement.

## Consequences
- Access self-heals on the next deploy after any spec-forced replacement.
- Every deploy re-issues these statements (cheap, idempotent).
- The app-role creation (`001`) stays `runOnChange:false` — creating a role isn't idempotent, so it
  runs once.
