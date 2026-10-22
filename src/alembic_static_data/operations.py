"""The ``op.load_static_data(...)`` migration operation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic.operations import MigrateOperation
from alembic.operations import Operations

from alembic_static_data.csv_loader import check_natural_keys_in_header
from alembic_static_data.csv_loader import check_unique_keys
from alembic_static_data.csv_loader import load_csv_rows
from alembic_static_data.errors import OfflineModeError
from alembic_static_data.upsert import UpsertResult
from alembic_static_data.upsert import check_columns
from alembic_static_data.upsert import coerce_row
from alembic_static_data.upsert import upsert_rows

log = logging.getLogger(__name__)


@Operations.register_operation("load_static_data")
class LoadStaticDataOp(MigrateOperation):
    """Idempotently load a CSV file into a table, keyed on natural keys. Never deletes rows."""

    def __init__(
        self, table_name: str, csv_path: str | Path, natural_keys: list[str], schema: str | None = None
    ) -> None:
        self.table_name = table_name
        self.csv_path = csv_path
        self.natural_keys = list(natural_keys)
        self.schema = schema

    @classmethod
    def load_static_data(
        cls,
        operations: Operations,
        table_name: str,
        csv_path: str | Path,
        natural_keys: list[str],
        schema: str | None = None,
    ) -> UpsertResult | None:
        """Issue ``op.load_static_data(table_name, csv_path, natural_keys)`` from a migration script.

        :param table_name: existing table to load into.
        :param csv_path: CSV file with a header row; header names must be column names of the table.
        :param natural_keys: columns identifying a row; must be in the CSV header and in the table.
        :param schema: optional schema of the table.
        """
        return cast("UpsertResult | None", operations.invoke(cls(table_name, csv_path, natural_keys, schema)))


@Operations.implementation_for(LoadStaticDataOp)
def load_static_data(operations: Operations, operation: LoadStaticDataOp) -> UpsertResult:
    if operations.migration_context.as_sql:
        raise OfflineModeError("op.load_static_data() needs a database connection and cannot run in offline mode")
    connection = operations.get_bind()
    table = sa.Table(operation.table_name, sa.MetaData(), schema=operation.schema, autoload_with=connection)
    return apply_static_data(connection, table, operation.csv_path, operation.natural_keys)


def apply_static_data(
    connection: sa.Connection, table: sa.Table, csv_path: str | Path, natural_keys: list[str]
) -> UpsertResult:
    """Validate the CSV against ``table`` and upsert it; shared by the operation and directly testable."""
    header, raw_rows = load_csv_rows(csv_path)
    check_natural_keys_in_header(header, natural_keys, csv_path)
    check_columns(table, header, natural_keys)
    check_unique_keys(raw_rows, natural_keys, csv_path)
    result = upsert_rows(connection, table, [coerce_row(row, table) for row in raw_rows], natural_keys)
    log.info("load_static_data %s from %s: %s", table.name, csv_path, result)
    return result
