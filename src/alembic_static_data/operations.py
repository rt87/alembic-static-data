"""The ``op.load_static_data(...)`` migration operation."""

from __future__ import annotations

import dataclasses
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
from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import checksum_for
from alembic_static_data.tracking import ensure_version_table
from alembic_static_data.tracking import record_load
from alembic_static_data.tracking import stored_checksum
from alembic_static_data.tracking import tracking_key
from alembic_static_data.tracking import version_table
from alembic_static_data.upsert import UpsertResult
from alembic_static_data.upsert import check_columns
from alembic_static_data.upsert import coerce_row
from alembic_static_data.upsert import delete_missing_rows
from alembic_static_data.upsert import upsert_rows

log = logging.getLogger(__name__)


@Operations.register_operation("load_static_data")
class LoadStaticDataOp(MigrateOperation):
    """Idempotently load a CSV file into a table, keyed on natural keys. Never deletes rows."""

    def __init__(
        self,
        table_name: str,
        csv_path: str | Path,
        natural_keys: list[str],
        schema: str | None = None,
        *,
        delete_missing: bool = False,
        track_checksum: bool = True,
        version_table_name: str = DEFAULT_VERSION_TABLE,
    ) -> None:
        self.table_name = table_name
        self.csv_path = csv_path
        self.natural_keys = list(natural_keys)
        self.schema = schema
        self.delete_missing = delete_missing
        self.track_checksum = track_checksum
        self.version_table_name = version_table_name

    @classmethod
    def load_static_data(
        cls,
        operations: Operations,
        table_name: str,
        csv_path: str | Path,
        natural_keys: list[str],
        schema: str | None = None,
        *,
        delete_missing: bool = False,
        track_checksum: bool = True,
        version_table_name: str = DEFAULT_VERSION_TABLE,
    ) -> UpsertResult | None:
        """Issue ``op.load_static_data(table_name, csv_path, natural_keys)`` from a migration script.

        :param table_name: existing table to load into.
        :param csv_path: CSV file with a header row; header names must be column names of the table.
        :param natural_keys: columns identifying a row; must be in the CSV header and in the table.
        :param schema: optional schema of the table, also used for the checksum table.
        :param delete_missing: when true, delete table rows whose natural key is absent from the CSV.
            Off by default: the operation otherwise never deletes anything.
        :param track_checksum: when true (the default), record the CSV's SHA-256 and skip the load entirely
            while that checksum is unchanged, so re-running a revision reloads only the files that changed.
        :param version_table_name: name of the checksum table, created on first use.
        """
        operation = cls(
            table_name,
            csv_path,
            natural_keys,
            schema,
            delete_missing=delete_missing,
            track_checksum=track_checksum,
            version_table_name=version_table_name,
        )
        return cast("UpsertResult | None", operations.invoke(operation))


@Operations.implementation_for(LoadStaticDataOp)
def load_static_data(operations: Operations, operation: LoadStaticDataOp) -> UpsertResult:
    if operations.migration_context.as_sql:
        raise OfflineModeError("op.load_static_data() needs a database connection and cannot run in offline mode")
    connection = operations.get_bind()
    table = sa.Table(operation.table_name, sa.MetaData(), schema=operation.schema, autoload_with=connection)
    if not operation.track_checksum:
        return apply_static_data(
            connection, table, operation.csv_path, operation.natural_keys, delete_missing=operation.delete_missing
        )

    # Checksum first: a missing file must fail the same way whether or not tracking is on.
    checksum = checksum_for(operation.csv_path)
    csv_key = tracking_key(operation.csv_path)
    versions = version_table(operation.version_table_name, operation.schema)
    ensure_version_table(connection, versions)
    if stored_checksum(connection, versions, operation.table_name, csv_key) == checksum:
        log.info("load_static_data %s from %s: skipped, checksum unchanged", table.name, operation.csv_path)
        return UpsertResult(skipped=True)

    result = apply_static_data(
        connection, table, operation.csv_path, operation.natural_keys, delete_missing=operation.delete_missing
    )
    record_load(connection, versions, operation.table_name, csv_key, checksum, result.rows)
    return result


def apply_static_data(
    connection: sa.Connection,
    table: sa.Table,
    csv_path: str | Path,
    natural_keys: list[str],
    *,
    delete_missing: bool = False,
) -> UpsertResult:
    """Validate the CSV against ``table`` and upsert it; shared by the operation and directly testable.

    Knows nothing about checksums: the caller decides whether this runs at all.
    """
    header, raw_rows = load_csv_rows(csv_path)
    check_natural_keys_in_header(header, natural_keys, csv_path)
    check_columns(table, header, natural_keys)
    check_unique_keys(raw_rows, natural_keys, csv_path)
    rows = [coerce_row(row, table) for row in raw_rows]
    result = upsert_rows(connection, table, rows, natural_keys)
    if delete_missing:
        result = dataclasses.replace(result, deleted=delete_missing_rows(connection, table, rows, natural_keys))
    log.info("load_static_data %s from %s: %s", table.name, csv_path, result)
    return result
