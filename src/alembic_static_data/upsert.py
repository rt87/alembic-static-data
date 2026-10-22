"""Portable (SQLAlchemy Core) upsert of CSV rows into a table, keyed on natural keys.

Deliberately dialect-neutral: SELECT by key, then INSERT or UPDATE, instead of ``ON CONFLICT`` / ``MERGE``.
It never deletes anything.
"""

from __future__ import annotations

import datetime
import decimal
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from alembic_static_data.errors import ColumnMismatchError
from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.errors import ValueCoercionError

_TRUE = {"true", "t", "1", "yes", "y"}
_FALSE = {"false", "f", "0", "no", "n"}


@dataclass(frozen=True)
class UpsertResult:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def check_columns(table: sa.Table, header: list[str], natural_keys: list[str]) -> None:
    """Raise unless every CSV column and every natural key exists in ``table``."""
    table_columns = [column.name for column in table.columns]
    unknown = [name for name in header if name not in table_columns]
    if unknown:
        raise ColumnMismatchError(
            f"CSV column(s) {unknown} not found in table {table.name!r} (columns: {table_columns})"
        )
    not_in_table = [key for key in natural_keys if key not in table_columns]
    if not_in_table:
        raise NaturalKeyError(f"Natural key column(s) {not_in_table} not found in table {table.name!r}")


def coerce_value(raw: str, column: sa.Column[Any]) -> Any:
    """Convert a CSV string to the Python type of ``column``; an empty cell becomes ``None``."""
    if raw == "":
        return None
    try:
        python_type = column.type.python_type
    except NotImplementedError:
        return raw
    try:
        if python_type is bool:
            return _to_bool(raw)
        if python_type is datetime.datetime:
            return datetime.datetime.fromisoformat(raw)
        if python_type is datetime.date:
            return datetime.date.fromisoformat(raw)
        if python_type in (int, float, decimal.Decimal):
            return python_type(raw)
    except (ValueError, decimal.InvalidOperation) as exc:
        raise ValueCoercionError(
            f"Cannot convert {raw!r} to {python_type.__name__} for column {column.table.name}.{column.name}"
        ) from exc
    return raw


def _to_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise ValueError(raw)


def coerce_row(row: dict[str, str], table: sa.Table) -> dict[str, Any]:
    """Coerce every cell of a CSV row to its column type."""
    return {name: coerce_value(raw, table.columns[name]) for name, raw in row.items()}


def build_select(table: sa.Table, natural_keys: list[str], row: dict[str, Any]) -> sa.Select[Any]:
    """SELECT the existing row matching ``row``'s natural key values."""
    return sa.select(table).where(*(table.columns[key] == row[key] for key in natural_keys))


def build_insert(table: sa.Table, row: dict[str, Any]) -> sa.Insert:
    return sa.insert(table).values(**row)


def build_update(table: sa.Table, natural_keys: list[str], row: dict[str, Any], changes: dict[str, Any]) -> sa.Update:
    """UPDATE only the changed non-key columns of the row matching the natural keys."""
    return sa.update(table).where(*(table.columns[key] == row[key] for key in natural_keys)).values(**changes)


def changed_columns(existing: sa.RowMapping, row: dict[str, Any], natural_keys: list[str]) -> dict[str, Any]:
    """Non-key CSV columns whose value differs from the existing database row."""
    return {name: value for name, value in row.items() if name not in natural_keys and existing[name] != value}


def upsert_rows(
    connection: sa.Connection, table: sa.Table, rows: list[dict[str, Any]], natural_keys: list[str]
) -> UpsertResult:
    """Insert missing rows, update changed ones, leave everything else (including rows not in ``rows``) alone."""
    inserted = updated = unchanged = 0
    for row in rows:
        existing = connection.execute(build_select(table, natural_keys, row)).mappings().first()
        if existing is None:
            connection.execute(build_insert(table, row))
            inserted += 1
            continue
        changes = changed_columns(existing, row, natural_keys)
        if changes:
            connection.execute(build_update(table, natural_keys, row, changes))
            updated += 1
        else:
            unchanged += 1
    return UpsertResult(inserted=inserted, updated=updated, unchanged=unchanged)
