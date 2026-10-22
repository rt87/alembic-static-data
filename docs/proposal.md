# Repeatable static data loading for Alembic

A design proposal, written to be read by someone who has not seen this repository. It states the problem,
what other tools do about it, the design this prototype settled on, and what is still open — including
whether this belongs in Alembic at all.

Status: prototype, private repository, nothing published. Nothing here has been discussed with the Alembic
maintainers yet.

## The problem

Alembic migrations run **once**. A revision is applied, its row lands in `alembic_version`, and it is never
executed again. That is exactly right for schema changes and wrong for *reference data* — currencies,
countries, tax rates, instrument groups, status codes — where the file describing the data keeps changing
while the schema does not.

Today a project has three options, all unsatisfying:

1. **Hand-write `op.bulk_insert()` per revision.** Each later correction needs a new revision, and the
   insert fails on a database where the row already exists.
2. **Write `op.execute()` with dialect-specific upserts.** `ON CONFLICT`, `MERGE` and `INSERT IGNORE` all
   differ, so the migration stops being portable.
3. **Load the data outside Alembic**, from a script or at application start, which loses the ordering
   guarantees and the transaction that migrations give you.

The underlying gap: Alembic has no concept of a migration that *should* re-run when its input changes.

## Prior art

| Tool | Mechanism | Relevant detail |
| --- | --- | --- |
| **Flyway** | Repeatable migrations (`R__` prefix) re-run whenever their checksum changes | Ordered by filename; the SQL itself must tolerate re-runs, typically via natural keys and `ON CONFLICT` |
| **Liquibase** | `loadData` change type plus `runOnChange` | Closest existing equivalent of this proposal |
| **Atlas** | Declarative schema, lint for destructive changes, computed down-migrations | Forward-only by default; seed data not addressed |
| **Alembic** | — | [sqlalchemy/alembic#620](https://github.com/sqlalchemy/alembic/issues/620): the maintainer's answer was that a small data migration is just a normal version file, and that there was no concept of a separate data feature |

Flyway's model is the one worth borrowing: *re-run when the content changes, never otherwise.*

## The design

One operation, provided by a plugin that Alembic discovers through the `alembic.plugins` entry point:

```python
def upgrade() -> None:
    op.load_static_data("currency", CSV_PATH, natural_keys=["code"])


def downgrade() -> None:
    op.forget_static_data("currency", CSV_PATH)
```

Five decisions carry the design:

**1. Keyed on natural keys, not surrogate ids.** The CSV names the business key (`code`), so the same file
loads into databases whose autoincrement ids differ. Rows are matched on that key: missing ones are
inserted, changed non-key columns are updated, identical rows are left alone.

**2. Dialect-neutral by construction.** `SELECT` by key, then `INSERT` or `UPDATE` — no `ON CONFLICT`, no
`MERGE`. One statement per row is slower than a bulk upsert and irrelevant at reference-data sizes; the
payoff is that the same migration runs unchanged on SQLite, Postgres, MySQL, Oracle and SQL Server.

**3. Never deletes by default.** Rows present in the table but absent from the CSV are kept, and columns
absent from the CSV header are never written. Deletion is opt-in per call (`delete_missing=True`). A data
loader that silently deletes production rows because someone shortened a file is not a tool anyone should
have to trust.

**4. The re-run trigger lives inside the operation.** The CSV's SHA-256 is recorded in a
`static_data_version` table; while it matches, the load is skipped entirely. No `env.py` hook and no
separate command, which means loads stay ordered by revision order and nothing new has to be wired up.
Alembic does offer a per-step hook (`EnvironmentContext.configure(on_version_apply=...)`), but it fires per
revision step rather than once per upgrade, so it does not give Flyway's "run repeatables at the end"
semantics anyway.

**5a. Environments select rows, not files.** Liquibase-style contexts in one reviewable file beat n copies
of the same data: a diff shows every environment side by side, shared rows are written once, and the
environment value rides on Alembic's existing `-x` mechanism rather than a new one.

**5. The checksum describes the file, not the table.** A row edited by hand in the database stays edited
until the CSV changes. This keeps the rule explainable in one sentence and avoids a `COUNT(*)` on every
load. The cost is an explicit `op.forget_static_data()` in `downgrade()`, since otherwise the recorded
checksum outlives the data and a later upgrade skips the reload.

## What exists

- `op.load_static_data(table, csv, natural_keys, schema=None, *, delete_missing=False, track_checksum=True,
  version_table_name="static_data_version")` returning `UpsertResult(inserted, updated, unchanged, deleted,
  skipped)`.
- `op.forget_static_data(table, csv=None, ...)` — the inverse of the bookkeeping, for `downgrade()`.
- Environment scoping: a CSV column names the environments each row belongs to, and the environment comes
  from Alembic's `-x` argument. The environment is part of the checksum key, and the same natural key may
  appear once per environment, which is how a row is overridden for dev.
- `register_operations()` for Alembic 1.17, which has no plugin system, and `exclude_from_autogenerate` to
  keep `--autogenerate` from proposing a `drop_table` for the bookkeeping table.
- Type coercion from CSV strings to each column's Python type; an empty cell becomes `NULL`.
- Errors that name the file, line, column or key, all under one `StaticDataError` base.
- A worked example (`examples/reference_data`) driven end to end by the test suite, and the suite itself on
  SQLite plus a real Postgres, across Alembic 1.17.0 through 1.20.1.

## What is deliberately missing

- **Offline (`--sql`) mode.** The operation has to read the database to decide what to insert, so it raises
  rather than emitting incomplete SQL.
- **Autogenerate integration.** Nothing detects that a table's data drifted from a CSV; this is a
  migration-time operation only.
- **Bulk loading.** One `SELECT` per row, as above.
- **Per-environment files or base/overlay layering.** Environment scoping itself is supported, but through a
  row filter in one file (see below), not through one file per environment.

## Open question: plugin or core?

Arguments for keeping it a separate package:

- It is opinionated in ways core probably should not be — hashing bytes, natural keys as the identity, the
  never-delete default.
- It can iterate at its own pace, and it needs no changes to Alembic.
- The maintainer's answer in #620 suggests no appetite for a data feature in core.

Arguments for proposing it upstream:

- The gap is structural, not project-specific: *every* project with reference data hits it, and the
  workarounds (dialect-specific upserts in `op.execute()`) are worse than a shared solution.
- Doing it well requires knowledge of Alembic internals that a plugin has to reverse-engineer. This
  prototype had to handle three different `alembic.plugins` loader behaviours across one year of releases
  (1.17 has no plugin system; 1.18.0–1.20.0 iterate the entry point; 1.20.1+ expect a module), which a
  core feature would simply not face.
- The licence is MIT, matching Alembic and SQLAlchemy, so the code could move upstream without a
  relicensing step.

A middle path: keep the plugin, and ask the maintainers only whether the *entry-point contract* and the
`on_version_apply` hook are intended to support this kind of extension. That is a smaller question than
"please adopt a data feature", and the answer shapes whether the plugin is standing on stable ground.

## Next steps

1. Decide plugin vs. upstream proposal (this document's main open question).
2. If upstream: open a **GitHub Discussion in the `Ideas` category** of `sqlalchemy/alembic`, linking this
   document rather than restating it. Verified 2026-10-22: the project sets `blank_issues_enabled: false`
   and its issue-template config routes feature proposals to Discussions/`Ideas` ("Use this for initial
   discussion for new features and suggestions"), with Gitter (`gitter.im/sqlalchemy/community`) for quick
   questions. Commenting on #620 would be the wrong channel twice over — it is closed and was last touched
   in February 2020.
3. If plugin: pick a public name, publish, and document the Alembic version matrix prominently, since the
   loader behaviour changed twice within a year.
