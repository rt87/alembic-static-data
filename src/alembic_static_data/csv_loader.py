"""Reading static data CSV files into plain row dicts (no database access)."""

from __future__ import annotations

import csv
from pathlib import Path

from alembic_static_data.errors import CsvFormatError
from alembic_static_data.errors import NaturalKeyError


def load_csv_rows(csv_path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read ``csv_path`` and return ``(header, rows)``; every cell is still a string.

    Raises ``CsvFormatError`` for a missing/empty file, a blank or duplicated header column, or a data row
    whose cell count differs from the header.
    """
    path = Path(csv_path)
    if not path.is_file():
        raise CsvFormatError(f"CSV file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise CsvFormatError(f"CSV file is empty (no header row): {path}")
        header = [name.strip() for name in header]
        _check_header(header, path)
        rows: list[dict[str, str]] = []
        for line_number, cells in enumerate(reader, start=2):
            if not cells:
                continue
            if len(cells) != len(header):
                raise CsvFormatError(f"{path}, line {line_number}: expected {len(header)} cells, got {len(cells)}")
            rows.append(dict(zip(header, cells, strict=True)))
    return header, rows


def _check_header(header: list[str], path: Path) -> None:
    if any(not name for name in header):
        raise CsvFormatError(f"{path}: header contains a blank column name: {header}")
    duplicates = sorted({name for name in header if header.count(name) > 1})
    if duplicates:
        raise CsvFormatError(f"{path}: header contains duplicate columns: {duplicates}")


def check_natural_keys_in_header(header: list[str], natural_keys: list[str], csv_path: str | Path) -> None:
    """Raise ``NaturalKeyError`` unless ``natural_keys`` is non-empty and all of it appears in ``header``."""
    if not natural_keys:
        raise NaturalKeyError("natural_keys must name at least one column")
    missing = [key for key in natural_keys if key not in header]
    if missing:
        raise NaturalKeyError(f"Natural key column(s) {missing} missing from CSV header {header} ({csv_path})")


def check_unique_keys(rows: list[dict[str, str]], natural_keys: list[str], csv_path: str | Path) -> None:
    """Raise ``NaturalKeyError`` if two CSV rows share the same natural key values."""
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        key = tuple(row[name] for name in natural_keys)
        if key in seen:
            raise NaturalKeyError(f"Duplicate natural key {dict(zip(natural_keys, key, strict=True))} in {csv_path}")
        seen.add(key)
