# alembic-static-data

[![Status: prototype](https://img.shields.io/badge/status-prototype-orange)](#not-done-yet)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![Alembic 1.17+](https://img.shields.io/badge/alembic-1.17%2B-ca4245)](#alembic-compatibility)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Coverage: 100%](https://img.shields.io/badge/coverage-100%25-brightgreen)](#development)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-2a6db2)](https://mypy-lang.org/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000)](https://github.com/psf/black)

An [Alembic](https://alembic.sqlalchemy.org/) plugin that loads **static reference data** (currencies, countries,
lookup tables, ...) from a CSV file in a migration, **idempotently**, keyed on **natural keys**. Alembic has no
built-in support for repeatable/seed data; this fills that gap.

Status: first prototype. One operation, no rerun tracking.

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

`op.load_static_data(table_name, csv_path, natural_keys, schema=None)`:

- Rows whose natural key is not in the table are **inserted**.
- Rows whose natural key exists and whose non-key values differ are **updated** (only the changed columns).
- Identical rows are left alone, so running it again changes nothing (idempotent).
- Rows in the table but absent from the CSV are **never deleted**. Columns absent from the CSV are never touched.
- The table must already exist (create it earlier in the migration). It is reflected from the live connection.
- Cells are converted to the column's Python type (`int`, `float`, `Decimal`, `bool`, `date`, `datetime` ISO format);
  an empty cell becomes `NULL`.
- The returned `UpsertResult(inserted, updated, unchanged)` is also logged at INFO.
- Uses plain SQLAlchemy Core (SELECT, then INSERT or UPDATE), no dialect-specific `ON CONFLICT`/`MERGE`.
- `csv_path` is resolved relative to the working directory (or pass an absolute `Path`).

Failures raise subclasses of `alembic_static_data.errors.StaticDataError` with a message naming the file, line,
column or key: a missing or empty CSV, a blank/duplicate header, a row with the wrong number of cells, a natural key
missing from the CSV header, a duplicate natural key within the CSV, a CSV column the table does not have, a value
that cannot be converted, and offline (`--sql`) mode, which is unsupported because the operation must read the
database.

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
uv run pytest        # includes coverage; pytest-randomly shuffles order each run
uv run pytest --testmon --no-cov   # fast inner loop: only tests affected by your edits
uv run pytest -n auto --no-cov     # parallel
```

CI lives in `.github/workflows/ci.yml` and is **deliberately disabled** (only `workflow_dispatch`) while CI
minutes are spoken for; uncomment the `push`/`pull_request` triggers to turn it on. It lints, type-checks,
and runs the suite on Python 3.11-3.14 plus one job per Alembic plugin-loading regime.

To check an older Alembic (the suite skips the two plugin-system tests below 1.18):

```bash
uv run --with "alembic==1.17.0" pytest -q --no-cov -W error::DeprecationWarning
```

## Not done yet

- **No rerun-on-checksum tracking.** The operation runs whenever the migration runs. Re-applying a changed CSV
  automatically (tracking a checksum per file) needs a design decision and is out of scope for this pass.
- **No Postgres test run.** Tests run against in-memory SQLite only; Docker is not available on the dev machine.
  TODO: add the same suite against Postgres (e.g. via `testcontainers`) before relying on it there.
- No `downgrade` behaviour: the operation is not reversible by design (it never deletes).
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

No license file has been chosen yet.
