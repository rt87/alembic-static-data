# Changelog

- 2026-10-22 — PR #13: refuse `delete_missing=True` together with `environment_column`, which silently deleted other environments' rows

- 2026-10-22 — #11 → PR #12: refuse to load unless the table guarantees the `natural_keys` match at most one row (`require_unique=False` opts out), and accept a single natural key as a plain string

- 2026-10-22 — #9 → PR #10: load a per-environment subset of one CSV via an environment column, with the environment taken from `alembic -x env=…` and made part of the checksum key

- 2026-10-22 — #4 → PR #5: add `op.forget_static_data()` so a `downgrade()` can delete the recorded checksum, which otherwise outlives the data and makes the next upgrade skip the reload

- 2026-10-22 — #1 → PR #3: skip a static-data load while its CSV's SHA-256 matches the one recorded in `static_data_version`, re-upsert when it changes, add opt-in `delete_missing=True`, keep the checksum table out of `--autogenerate` via `exclude_from_autogenerate`, and cover it all on a real Postgres through testcontainers
