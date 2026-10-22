# Changelog

- 2026-10-22 — #1 → PR #3: skip a static-data load while its CSV's SHA-256 matches the one recorded in `static_data_version`, re-upsert when it changes, add opt-in `delete_missing=True`, keep the checksum table out of `--autogenerate` via `exclude_from_autogenerate`, and cover it all on a real Postgres through testcontainers
