# Repeatable static data loading for Alembic

A design proposal, written to be read by someone who has not seen this repository. It states the problem,
what other tools do about it, the design this prototype settled on, and what is still open — including
whether this belongs in Alembic at all.

Status: prototype (`0.1.0a1`), public repository, published to TestPyPI only — not to PyPI, and not under a
name anyone should depend on yet. Nothing here has been raised with the Alembic maintainers; the section below
collects what they have already said on their own, without being asked.

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
| **Alembic** | — | No seed-data concept; the maintainer has declined one three times since 2020 and points at third-party libraries instead. See the next section |

Flyway's model is the one worth borrowing: *re-run when the content changes, never otherwise.*

## What the maintainers have already said

Read this before proposing anything, because it was all said unprompted and it is consistent. Checked 2026-10-22.

- **[issue #620](https://github.com/sqlalchemy/alembic/issues/620)** (closed, last touched February 2020) — a
  small data migration is just a normal version file; there is no concept of a separate data feature.
- **[discussion #950](https://github.com/sqlalchemy/alembic/discussions/950)**, "Does alembic have repeatable
  migrations?", asked with a link to Flyway's repeatable migrations — the exact question this document asks.
  Answered the same day: *"this is not a first class feature of Alembic"*, with a pointer to the replaceable-objects
  cookbook recipe and to [alembic-utils](https://github.com/olirice/alembic_utils) as the supported third-party
  implementation of that pattern.
- **[discussion #972](https://github.com/sqlalchemy/alembic/discussions/972)**, "Document how to do data
  migrations", **still open**: *"I dont have any fixed method of doing data migrations with alembic ... in this
  discussion if folks want to capture things that are safe to document (that is, won't mislead people into a poor /
  rigid architecture ...) we can maybe figure something out."* A standing, unfulfilled invitation to supply
  documentation content — and the only place where contributing here is asked for rather than unsolicited.
- **[discussion #1872](https://github.com/sqlalchemy/alembic/discussions/1872)** (September 2026), on what the
  `alembic.plugins` entry point is for: *"the 'plugins' system is decidedly not for dialect registration, it's for
  things that extend Alembic itself, typically though not necessarily in a dialect agnostic way."* A
  dialect-agnostic extension of Alembic itself is precisely what this is, so the entry point is being used as
  intended. The same answer describes the plugin system's own history bluntly: the refactor was done for a CHECK
  constraint plugin that *"failed immediately once we released and we basically had to almost completely revert it,
  it's now useless cruft in the codebase that nobody uses."*

Two conclusions follow. Asking core to adopt a data feature is pre-answered and would be the fifth time; and the
one thing a plugin cannot settle for itself — whether the entry point is meant to carry this — is already
answered yes, in public, in writing.

## The design

One operation, provided by a plugin that Alembic discovers through the `alembic.plugins` entry point:

```python
def upgrade() -> None:
    op.load_static_data("currency", CSV_PATH, natural_keys=["code"])


def downgrade() -> None:
    op.forget_static_data("currency", CSV_PATH)
```

Six decisions carry the design:

**1. Keyed on natural keys, not surrogate ids.** The CSV names the business key (`code`), so the same file
loads into databases whose autoincrement ids differ. Rows are matched on that key: missing ones are
inserted, changed non-key columns are updated, identical rows are left alone. The table must *prove* that
those columns are unique — a primary key, unique constraint or unique index covered by them — because
otherwise one CSV row overwrites every matching table row while the result reports a single update.

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

**5. Environments select rows, not files.** Liquibase-style contexts in one reviewable file beat n copies
of the same data: a diff shows every environment side by side, shared rows are written once, and the
environment value rides on Alembic's existing `-x` mechanism rather than a new one.

**6. The checksum describes the file, not the table.** A row edited by hand in the database stays edited
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
  core feature would simply not face. Note that the 1.18–1.20 behaviour was a bug rather than a contract: it
  was reported as [#1873](https://github.com/sqlalchemy/alembic/issues/1873) and fixed within two days, in 1.20.1.
  Supporting the broken releases anyway is this project's own choice, so it is not a question for anyone upstream.
- The licence is MIT, matching Alembic and SQLAlchemy, so the code could move upstream without a
  relicensing step.

The middle path this document used to propose — ask only whether the entry-point contract is meant to
support this kind of extension — no longer has a question in it: #1872 answers it and #1873 settled the loader's
shape. So the honest position is that this stays a plugin, and the remaining reason to approach the maintainers is
not to ask permission but to offer something: a worked pattern for the documentation gap #972 is explicitly still
looking for, and the fact that the plugin system now has a third-party user.

## Next steps

1. **Do not open a new `Ideas` discussion proposing a data feature.** It has been declined in #620 and #950 and,
   by implication, in #972, each time with the same reasoning. A fifth asking adds nothing and spends the
   maintainers' patience on a question whose answer is already in writing.
2. **Comment on the open [#972](https://github.com/sqlalchemy/alembic/discussions/972) instead**, which asked for
   exactly this and never got it: a short description of the checksum-plus-natural-key pattern, the trade-offs that
   make it safe (never delete by default, prove the keys are unique, dialect-neutral statements), and a link here,
   framed as documentation content others can copy by hand rather than as a request. Mention the plugin as one
   implementation, once, without pitching it.
3. **Do that only after a real PyPI release.** The package currently exists on TestPyPI alone, so a link offers
   readers something they cannot install, which makes the post an announcement of intent rather than a contribution.
4. Keep documenting the Alembic version matrix prominently, since the loader behaviour changed twice within a year.

For quick questions the project points at Gitter (`gitter.im/sqlalchemy/community`); its issue templates set
`blank_issues_enabled: false` and route feature proposals to Discussions/`Ideas`. Verified 2026-10-22.
