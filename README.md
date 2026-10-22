# alembic-static-data

[![Status: prototype](https://img.shields.io/badge/status-prototype-orange)](#not-done-yet)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Alembic 1.17+](https://img.shields.io/badge/alembic-1.17%2B-ca4245)](#alembic-compatibility)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Coverage: 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)](#development)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000)](https://github.com/psf/black)
[![License: MIT](https://img.shields.io/badge/license-MIT-750014)](LICENSE)

An [Alembic](https://alembic.sqlalchemy.org/) plugin that loads **static reference data** (currencies, countries,
lookup tables, ...) from a CSV file in a migration, **idempotently**, keyed on **natural keys**. Alembic has no
built-in support for repeatable/seed data; this fills that gap.

Status: first prototype. One operation, with checksum-based rerun tracking.

## Usage

```python
# in a migration script
from alembic import op


def upgrade() -> None:
    op.load_static_data("currency", "data/currency.csv", natural_keys=["code"])
```

`currency.csv`:

```csv
code,name,decimals
EUR,Euro,2
JPY,Yen,0
```

`op.load_static_data(table_name, csv_path, natural_keys, schema=None, *, delete_missing=False,
track_checksum=True, version_table_name="static_data_version", environment_column=None,
environment_key="env", require_unique=True)`:

- **The natural keys must identify at most one row, and the table has to prove it.** The load refuses
  unless a primary key, `UniqueConstraint` or unique `Index` is covered by the natural keys, because the
  upsert's `UPDATE ... WHERE <keys>` would otherwise overwrite every matching row while reporting one. A
  unique key only has to be a *subset* of the natural keys: uniqueness on `code` justifies
  `natural_keys=["code", "name"]`, but a unique constraint on `(code, name)` does **not** justify
  `natural_keys=["code"]`. `require_unique=False` opts out, for schemas where uniqueness is enforced
  somewhere this cannot see.
- A single natural key may be given as a plain string: `natural_keys="code"`.
- Rows whose natural key is not in the table are **inserted**.
- Rows whose natural key exists and whose non-key values differ are **updated** (only the changed columns).
- Identical rows are left alone, so running it again changes nothing (idempotent).
- Rows in the table but absent from the CSV are **never deleted** unless you pass `delete_missing=True`.
  Columns absent from the CSV are never touched either way.
- The table must already exist (create it earlier in the migration). It is reflected from the live connection.
- Cells are converted to the column's Python type (`int`, `float`, `Decimal`, `bool`, `date`, `datetime` ISO format);
  an empty cell becomes `NULL`.
- The returned `UpsertResult(inserted, updated, unchanged, deleted, skipped)` is also logged at INFO.
- Uses plain SQLAlchemy Core (SELECT, then INSERT or UPDATE), no dialect-specific `ON CONFLICT`/`MERGE`.
- `csv_path` is handed straight to `open()`, so a relative path resolves against the **working directory**,
  not the migration file. Build an absolute path from the revision's own `__file__` instead — see the
  worked example; `env.py` cannot hold that constant, because Alembic loads it by path rather than as an
  importable module.

Failures raise subclasses of `alembic_static_data.errors.StaticDataError` with a message naming the file, line,
column or key: a missing or empty CSV, a blank/duplicate header, a row with the wrong number of cells, a natural key
missing from the CSV header, a duplicate natural key within the CSV, a CSV column the table does not have, a value
that cannot be converted, and offline (`--sql`) mode, which is unsupported because the operation must read the
database.

## Environment scoping

One CSV can carry rows for several environments. Add a column naming them, and pass the environment on the
command line:

```csv
code,name,decimals,_env
EUR,Euro,2,
JPY,Yen,0,
XTS,Test currency,2,dev
XXX,Sandbox,2,dev;staging
```

```python
op.load_static_data("currency", CSV_PATH, natural_keys=["code"], environment_column="_env")
```

```bash
alembic -x env=prod upgrade head     # loads EUR, JPY
alembic -x env=dev upgrade head      # loads EUR, JPY, XTS, XXX
```

- A **blank cell means the row applies everywhere**; otherwise the cell is a `;`-separated list (`,` belongs
  to the CSV), compared case-insensitively after stripping.
- The column is **not a table column**: it is removed before the upsert, and it may not be a natural key.
- The **same natural key may appear once per environment**, which is how you override a row for dev. Two rows
  for the *selected* environment are still a duplicate-key error.
- The environment comes from Alembic's own `-x` mechanism, read with `get_x_argument()`. The key defaults to
  `env` and is configurable via `environment_key=`.
- Rows for other environments are **ignored, not deleted** — scoping selects what to load, it does not
  reconcile what is already there. `delete_missing=True` is therefore **refused** together with
  `environment_column`: the filtered CSV omits every other environment's rows, the table has no environment
  column to tell them apart, and deleting what looks missing would wipe them. Give each environment its own
  database if you need the CSV to be authoritative.
- Configuring `environment_column=` with no environment available is an error (`EnvironmentNotSetError`),
  not a reason to quietly load a subset.

The environment is **part of the checksum key**, so loading the same file for a second environment is not
skipped, and `op.forget_static_data(..., environment="dev")` forgets just that one.

## Worked example

[`examples/reference_data`](examples/reference_data) is a complete, runnable Alembic project using this
plugin — `alembic.ini`, `env.py`, two revisions and a CSV. `tests/test_example_project.py` drives it through
`alembic.command`, so it is verified rather than decorative, and it is the only place the plugin is
exercised the way a user meets it: entry-point discovery inside a real `env.py`, a real `--autogenerate`
run, and `alembic downgrade base && alembic upgrade head` putting the data back.

## Rerun tracking

By default each load records its CSV's SHA-256 in a `static_data_version` table (created on first use, in the
same schema as the table being loaded, next to Alembic's `alembic_version`), and **skips the load entirely
while that checksum is unchanged**:

```python
op.load_static_data("currency", "data/currency.csv", natural_keys=["code"])
# first run:  UpsertResult(inserted=2)
# re-run:     UpsertResult(skipped=True)      - file unchanged, nothing read, nothing written
# after edit: UpsertResult(updated=1)         - checksum differs, so the upsert runs again
```

This is what makes a revision safe to re-run when its data file changes, which is Alembic's missing
repeatable-migration behaviour (issue #1). Notes on the design:

- The trigger lives **inside the operation**, not in an `env.py` hook or a separate command, so data loads
  stay ordered by revision order and nothing new has to be wired up.
- The checksum describes the **file**, not the table. A row edited by hand in the database stays edited until
  the CSV itself changes; `track_checksum=False` restores the old always-upsert behaviour.
- Bytes are hashed, not parsed rows, so re-indenting a CSV counts as a change (the following load is then a
  no-op upsert). Line endings count too: the same file as CRLF and as LF hash differently.
- One row per `(table_name, csv_path)` pair, keyed on the path **as written** in the migration, with
  separators normalised - so a relative path stays stable across checkouts, and the same CSV loaded into two
  tables is tracked separately.
- `version_table_name=` renames the table; loads tracked in different tables do not see each other.
- One row per `(table_name, csv_path, environment)`; see Environment scoping above.

### Downgrades

The recorded checksum outlives the data, so a `downgrade()` that removes the rows must also forget the load,
or the next upgrade sees a matching checksum and **skips the reload**:

```python
def upgrade() -> None:
    op.load_static_data("currency", "data/currency.csv", natural_keys=["code"])


def downgrade() -> None:
    op.forget_static_data("currency", "data/currency.csv")
```

`op.forget_static_data(table_name, csv_path=None, schema=None, *, version_table_name="static_data_version",
environment=None)` deletes only bookkeeping, never table data, and returns how many recorded loads it forgot.
Omit `csv_path` to forget every file recorded for that table, and `environment` to forget every environment. It never creates the checksum table, and forgetting something that
was never recorded returns `0` rather than failing.

### Autogenerate

The checksum table exists in the database but not in your `target_metadata`, so
`alembic revision --autogenerate` would otherwise propose dropping it. Exclude it in `env.py`:

```python
from alembic_static_data import exclude_from_autogenerate

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_name=exclude_from_autogenerate,
)
```

For a renamed table, bind the name first:
`functools.partial(exclude_from_autogenerate, version_table_names=["my_static_data"])`. If you already have
an `include_name`, call both and `and` the results.

## Install

Requires **Python 3.11+** and **Alembic 1.17 or newer**.

```bash
pip install git+https://github.com/rt87/alembic-static-data   # repo is private for now
# or, from a checkout
uv pip install -e .
```

On **Alembic 1.18 and newer** Alembic discovers the plugin through the `alembic.plugins` entry point
automatically; no `env.py` change is needed, and importing Alembic registers `op.load_static_data`.

On **Alembic 1.17**, which has no plugin system, register the operation from `env.py`:

```python
from alembic_static_data import register_operations

register_operations()
```

That call is idempotent and harmless on every version, so it is safe to keep in an `env.py` that has to run
against a range of Alembic releases.

## Development

```bash
uv sync
uv run ruff check .
uv run black .       # formats at line-length 120; `blackd` is installed for editors
uv run mypy          # strict, over src/ and tests/
uv run vermin -t=3.11- --violations --no-tips src tests   # does the code really still fit 3.11?
uv run pytest        # includes coverage; pytest-randomly shuffles order each run
uv run pytest --testmon --no-cov   # fast inner loop: only tests affected by your edits
uv run pytest -n auto --no-cov     # parallel
```

CI lives in `.github/workflows/ci.yml` and is **deliberately disabled** (only `workflow_dispatch`) while CI
minutes are spoken for; uncomment the `push`/`pull_request` triggers to turn it on. It lints, type-checks,
and runs the suite on Python 3.11-3.14 plus one job per Alembic plugin-loading regime.

The suite covers in-memory SQLite and, via `testcontainers`, a real Postgres 17 - the latter is the only way
to exercise named schemas and server-side `Numeric`. The Postgres tests skip themselves when Docker is not
reachable, so `uv run pytest` works either way.

To check an older Alembic (the suite skips the two plugin-system tests below 1.18):

```bash
uv run --with "alembic==1.17.0" pytest -q --no-cov -W error::DeprecationWarning
```

## Not done yet

- The load is not reversible by design (it never deletes data), so a `downgrade()` that removes rows has to
  call `op.forget_static_data()` itself - see Downgrades above.
- Deleting rows is opt-in per call (`delete_missing=True`) and is skipped along with everything else when the
  checksum matches, so it is not a reconciliation mechanism.
- No offline (`--sql`) support.
- Rows are processed one at a time (one SELECT each), which is fine for reference data but not for large files.

## Alembic compatibility

Alembic changed plugin loading twice in the past year, so the entry point has to cope with three regimes:

| Alembic | Released | Plugin system | How the operation gets registered |
| --- | --- | --- | --- |
| 1.17.x | 2025-10 | none | `register_operations()` in `env.py` |
| 1.18.0 – 1.20.0 | 2026-01 – 2026-09 | yes, loader iterates `entrypoint.load()` | entry point, automatically |
| 1.20.1+ | 2026-10 | yes, loader expects one module with `setup()` | entry point, automatically |

1.18.0 was the first release with a plugin system, and through 1.20.0 its loader ran
`for mod in entrypoint.load()`, contradicting the docs, which say to point the entry point at a single module
with `setup()`. A plain module therefore raises `TypeError` while `import alembic` is running. 1.20.1 fixed the
loader (Alembic #1873) to accept a module, and accepts the old collection form only with a deprecation warning.

`alembic_static_data.plugin` satisfies both: it is a module with `setup()` whose type also iterates as a
one-element collection containing the module itself (`_PluginModule`). New Alembic takes the module branch,
older Alembic iterates and arrives at the same module, and neither path emits a warning.

## License

[MIT](LICENSE), matching Alembic and SQLAlchemy, so this code can move upstream without a relicensing step.
