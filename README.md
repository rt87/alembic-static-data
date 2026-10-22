# alembic-static-data

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

Requires Python 3.14+ and **Alembic 1.20.1 or newer** (1.20.0 has a plugin-loading bug, see below).

```bash
pip install git+https://github.com/rt87/alembic-static-data   # repo is private for now
# or, from a checkout
uv pip install -e .
```

Alembic discovers the plugin through the `alembic.plugins` entry point automatically; no `env.py` change is needed.
Importing Alembic registers `op.load_static_data`.

## Development

```bash
uv sync
uv run ruff check .
uv run pytest        # includes coverage
```

## Not done yet

- **No rerun-on-checksum tracking.** The operation runs whenever the migration runs. Re-applying a changed CSV
  automatically (tracking a checksum per file) needs a design decision and is out of scope for this pass.
- **No Postgres test run.** Tests run against in-memory SQLite only; Docker is not available on the dev machine.
  TODO: add the same suite against Postgres (e.g. via `testcontainers`) before relying on it there.
- No `downgrade` behaviour: the operation is not reversible by design (it never deletes).
- No offline (`--sql`) support.
- Rows are processed one at a time (one SELECT each), which is fine for reference data but not for large files.

## Note on Alembic 1.20.0

In Alembic 1.20.0 the `alembic.plugins` entry point loader iterated over the loaded object
(`for mod in entrypoint.load()`), contradicting its own docs, which say to point at a single module with
`setup()`. 1.20.1 (Alembic #1873) accepts a module and only warns about the old collection form. This plugin
uses the module form and therefore depends on `alembic>=1.20.1`.

## License

No license file has been chosen yet.
