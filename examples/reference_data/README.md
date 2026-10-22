# Worked example: loading currency reference data

A complete, runnable Alembic project using `alembic-static-data`. The repository's
`tests/test_example_project.py` drives exactly this project through `alembic.command`, so it is verified
rather than decorative.

## Run it

```bash
cd examples/reference_data
uv run --project ../.. alembic upgrade head     # creates the table, loads 4 currencies
uv run --project ../.. alembic upgrade head     # nothing: already at head
```

Then edit `data/currency.csv`, point the revision at your change and re-run it:

```bash
uv run --project ../.. alembic stamp 0001       # pretend 0002 has not run
uv run --project ../.. alembic upgrade head     # checksum differs -> reloads, only changed rows are written
```

A full round trip puts the data back, because `downgrade()` forgets the recorded checksum:

```bash
uv run --project ../.. alembic downgrade base
uv run --project ../.. alembic upgrade head
```

Against Postgres instead of the bundled SQLite file, override the URL:

```bash
uv run --project ../.. alembic -x url=postgresql+psycopg://user:pw@localhost/db upgrade head
```

(The example reads `sqlalchemy.url` from `alembic.ini`; `-x` is shown here because that is how the test
injects its own URL — adapt `env.py` if you want `-x` support in a real project.)

## What to look at

| File | Why it matters |
| --- | --- |
| `migrations/env.py` | `register_operations()` for Alembic < 1.18, and `include_name=exclude_from_autogenerate` so `--autogenerate` does not propose dropping `static_data_version`. |
| `migrations/versions/0002_load_currencies.py` | The load itself, and the `op.forget_static_data()` in `downgrade()` without which a later upgrade would silently skip the reload. |
| `alembic.ini` | Logs `alembic_static_data` at INFO, so each run prints its `UpsertResult`. |

## Two things the example makes concrete

**CSV paths resolve against the working directory.** `op.load_static_data()` hands a relative path straight
to `open()`, so `"data/currency.csv"` only works when `alembic` is run from the project root. The revision
therefore builds an absolute path from its own `__file__`. Importing a shared constant from `env.py` is not
an option: Alembic loads `env.py` by path, so it is not an importable module.

**Offline mode is refused, loudly.** `alembic upgrade head --sql` raises `OfflineModeError` instead of
emitting incomplete SQL, because the operation has to read the database to know what to insert.
