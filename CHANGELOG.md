# Changelog

- 2026-10-22 — #1 → PR #3: skip a static-data load while its CSV's SHA-256 matches the one recorded in `static_data_version`, re-upsert when it changes, and add opt-in `delete_missing=True`
