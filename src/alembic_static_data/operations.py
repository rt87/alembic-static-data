"""The ``op.load_static_data(...)`` migration operation."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import sqlalchemy as sa
from alembic.operations import MigrateOperation
from alembic.operations import Operations

from alembic_static_data.csv_loader import check_natural_keys_in_header
from alembic_static_data.csv_loader import check_unique_keys
from alembic_static_data.csv_loader import load_csv_rows
from alembic_static_data.csv_loader import select_environment_rows
from alembic_static_data.errors import EnvironmentNotSetError
from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.errors import OfflineModeError
from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import UNSCOPED
from alembic_static_data.tracking import checksum_for
from alembic_static_data.tracking import ensure_version_table
from alembic_static_data.tracking import forget_load
from alembic_static_data.tracking import record_load
from alembic_static_data.tracking import stored_checksum
from alembic_static_data.tracking import tracking_key
from alembic_static_data.tracking import version_table
from alembic_static_data.tracking import version_table_exists
from alembic_static_data.upsert import UpsertResult
from alembic_static_data.upsert import check_columns
from alembic_static_data.upsert import check_natural_keys_unique
from alembic_static_data.upsert import coerce_row
from alembic_static_data.upsert import delete_missing_rows
from alembic_static_data.upsert import upsert_rows

log = logging.getLogger(__name__)


DEFAULT_ENVIRONMENT_KEY = "env"


def resolve_environment(operations: Operations, environment_key: str) -> str | None:
    """The environment from Alembic's ``-x`` arguments, e.g. ``alembic -x env=prod upgrade head``.

    Returns ``None`` when there is nothing to read it from: either no ``-x`` argument was given, or the
    migration runs without an ``EnvironmentContext`` at all (as in a test that configures a
    ``MigrationContext`` directly). ``get_x_argument(as_dictionary=True)`` exists from Alembic 1.13.1.
    """
    environment_context = getattr(operations.migration_context, "environment_context", None)
    if environment_context is None:
        return None
    arguments = environment_context.get_x_argument(as_dictionary=True)
    value = arguments.get(environment_key)
    return value or None


@Operations.register_operation("load_static_data")
class LoadStaticDataOp(MigrateOperation):
    """Idempotently load a CSV file into a table, keyed on natural keys. Never deletes rows."""

    def __init__(
        self,
        table_name: str,
        csv_path: str | Path,
        natural_keys: str | Sequence[str],
        schema: str | None = None,
        *,
        delete_missing: bool = False,
        track_checksum: bool = True,
        version_table_name: str = DEFAULT_VERSION_TABLE,
        environment_column: str | None = None,
        environment_key: str = DEFAULT_ENVIRONMENT_KEY,
        require_unique: bool = True,
    ) -> None:
        self.table_name = table_name
        self.csv_path = csv_path
        # A bare string is the common single-key case. Without this it would iterate into characters and
        # report a nonsensical ['c', 'o', 'd', 'e'].
        self.natural_keys = [natural_keys] if isinstance(natural_keys, str) else list(natural_keys)
        self.schema = schema
        self.delete_missing = delete_missing
        self.track_checksum = track_checksum
        self.version_table_name = version_table_name
        self.environment_column = environment_column
        self.environment_key = environment_key
        self.require_unique = require_unique

    @classmethod
    def load_static_data(
        cls,
        operations: Operations,
        table_name: str,
        csv_path: str | Path,
        natural_keys: str | Sequence[str],
        schema: str | None = None,
        *,
        delete_missing: bool = False,
        track_checksum: bool = True,
        version_table_name: str = DEFAULT_VERSION_TABLE,
        environment_column: str | None = None,
        environment_key: str = DEFAULT_ENVIRONMENT_KEY,
        require_unique: bool = True,
    ) -> UpsertResult | None:
        """Issue ``op.load_static_data(table_name, csv_path, natural_keys)`` from a migration script.

        :param table_name: existing table to load into.
        :param csv_path: CSV file with a header row; header names must be column names of the table.
        :param natural_keys: columns identifying a row; must be in the CSV header and in the table. A single
            column may be given as a plain string.
        :param schema: optional schema of the table, also used for the checksum table.
        :param delete_missing: when true, delete table rows whose natural key is absent from the CSV.
            Off by default: the operation otherwise never deletes anything.
        :param track_checksum: when true (the default), record the CSV's SHA-256 and skip the load entirely
            while that checksum is unchanged, so re-running a revision reloads only the files that changed.
        :param version_table_name: name of the checksum table, created on first use.
        :param environment_column: name of a CSV column naming the environments a row belongs to, e.g.
            ``"_env"``. A blank cell means the row applies everywhere, otherwise it is a ``;``-separated
            list compared case-insensitively. The column is not a table column: it is removed before the
            upsert. Rows for other environments are ignored entirely, so the same natural key may appear
            once per environment.
        :param environment_key: the ``-x`` key the environment is read from, i.e. ``alembic -x env=prod``.
        :param require_unique: when true (the default), refuse to load unless the table guarantees that the
            natural keys match at most one row. Set it to false only when uniqueness is enforced somewhere
            this cannot see, such as a partial index - the upsert then may overwrite several rows per CSV row.
        """
        operation = cls(
            table_name,
            csv_path,
            natural_keys,
            schema,
            delete_missing=delete_missing,
            track_checksum=track_checksum,
            version_table_name=version_table_name,
            environment_column=environment_column,
            environment_key=environment_key,
            require_unique=require_unique,
        )
        return cast("UpsertResult | None", operations.invoke(operation))


@Operations.register_operation("forget_static_data")
class ForgetStaticDataOp(MigrateOperation):
    """Forget that a CSV was loaded, so the next ``load_static_data`` runs again. Touches no data."""

    def __init__(
        self,
        table_name: str,
        csv_path: str | Path | None = None,
        schema: str | None = None,
        *,
        version_table_name: str = DEFAULT_VERSION_TABLE,
        environment: str | None = None,
    ) -> None:
        self.table_name = table_name
        self.csv_path = csv_path
        self.schema = schema
        self.version_table_name = version_table_name
        self.environment = environment

    @classmethod
    def forget_static_data(
        cls,
        operations: Operations,
        table_name: str,
        csv_path: str | Path | None = None,
        schema: str | None = None,
        *,
        version_table_name: str = DEFAULT_VERSION_TABLE,
        environment: str | None = None,
    ) -> int:
        """Issue ``op.forget_static_data(table_name, csv_path)`` from a migration script.

        The inverse of :meth:`LoadStaticDataOp.load_static_data`'s bookkeeping, for a ``downgrade()`` that
        removes the data: without it the recorded checksum outlives the rows and the next upgrade skips the
        load. Deletes only bookkeeping, never table data.

        :param table_name: table whose recorded load should be forgotten.
        :param csv_path: the file to forget; omit it to forget every file recorded for this table.
        :param schema: optional schema, as passed to the load.
        :param version_table_name: name of the checksum table, as passed to the load.
        :param environment: forget only this environment's record; omit it to forget every environment.
        :return: how many recorded loads were forgotten; ``0`` if there was nothing to forget.
        """
        operation = cls(table_name, csv_path, schema, version_table_name=version_table_name, environment=environment)
        return cast("int", operations.invoke(operation))


@Operations.implementation_for(ForgetStaticDataOp)
def forget_static_data(operations: Operations, operation: ForgetStaticDataOp) -> int:
    if operations.migration_context.as_sql:
        raise OfflineModeError("op.forget_static_data() needs a database connection and cannot run in offline mode")
    connection = operations.get_bind()
    # Deliberately does not create the table: a downgrade must not add one, and nothing is recorded anyway.
    if not version_table_exists(connection, operation.version_table_name, operation.schema):
        log.info(
            "forget_static_data %s: no %s table, nothing to do", operation.table_name, operation.version_table_name
        )
        return 0
    versions = version_table(operation.version_table_name, operation.schema)
    csv_key = tracking_key(operation.csv_path) if operation.csv_path is not None else None
    forgotten = forget_load(connection, versions, operation.table_name, csv_key, operation.environment)
    log.info("forget_static_data %s from %s: forgot %d", operation.table_name, operation.csv_path, forgotten)
    return forgotten


@Operations.implementation_for(LoadStaticDataOp)
def load_static_data(operations: Operations, operation: LoadStaticDataOp) -> UpsertResult:
    if operations.migration_context.as_sql:
        raise OfflineModeError("op.load_static_data() needs a database connection and cannot run in offline mode")
    connection = operations.get_bind()
    table = sa.Table(operation.table_name, sa.MetaData(), schema=operation.schema, autoload_with=connection)
    environment = None
    if operation.environment_column is not None:
        environment = resolve_environment(operations, operation.environment_key)
        if environment is None:
            raise EnvironmentNotSetError(
                f"environment_column={operation.environment_column!r} was given, but no environment was "
                f"found: pass it as `alembic -x {operation.environment_key}=<name> upgrade ...`"
            )

    if not operation.track_checksum:
        return apply_static_data(
            connection,
            table,
            operation.csv_path,
            operation.natural_keys,
            delete_missing=operation.delete_missing,
            environment_column=operation.environment_column,
            environment=environment,
            require_unique=operation.require_unique,
        )

    # Checksum first: a missing file must fail the same way whether or not tracking is on.
    checksum = checksum_for(operation.csv_path)
    csv_key = tracking_key(operation.csv_path)
    # The environment is part of the key: the same bytes select different rows per environment.
    tracked_environment = environment if environment is not None else UNSCOPED
    versions = version_table(operation.version_table_name, operation.schema)
    ensure_version_table(connection, versions)
    if stored_checksum(connection, versions, operation.table_name, csv_key, tracked_environment) == checksum:
        log.info(
            "load_static_data %s from %s (environment %r): skipped, checksum unchanged",
            table.name,
            operation.csv_path,
            tracked_environment,
        )
        return UpsertResult(skipped=True)

    result = apply_static_data(
        connection,
        table,
        operation.csv_path,
        operation.natural_keys,
        delete_missing=operation.delete_missing,
        environment_column=operation.environment_column,
        environment=environment,
        require_unique=operation.require_unique,
    )
    record_load(
        connection,
        versions,
        operation.table_name,
        csv_key,
        checksum,
        result.rows,
        environment=tracked_environment,
    )
    return result


def apply_static_data(
    connection: sa.Connection,
    table: sa.Table,
    csv_path: str | Path,
    natural_keys: list[str],
    *,
    delete_missing: bool = False,
    environment_column: str | None = None,
    environment: str | None = None,
    require_unique: bool = True,
) -> UpsertResult:
    """Validate the CSV against ``table`` and upsert it; shared by the operation and directly testable.

    Knows nothing about checksums: the caller decides whether this runs at all.
    """
    header, raw_rows = load_csv_rows(csv_path)
    if environment_column is not None and environment is not None:
        if environment_column in natural_keys:
            raise NaturalKeyError(
                f"environment column {environment_column!r} cannot also be a natural key ({csv_path})"
            )
        header, raw_rows = select_environment_rows(header, raw_rows, environment_column, environment, csv_path)
    check_natural_keys_in_header(header, natural_keys, csv_path)
    check_columns(table, header, natural_keys)
    if require_unique:
        check_natural_keys_unique(table, natural_keys)
    # After filtering on purpose: the same natural key may appear once per environment, which is only a
    # duplicate if two of those rows apply to the environment being loaded.
    check_unique_keys(raw_rows, natural_keys, csv_path)
    rows = [coerce_row(row, table) for row in raw_rows]
    result = upsert_rows(connection, table, rows, natural_keys)
    if delete_missing:
        result = dataclasses.replace(result, deleted=delete_missing_rows(connection, table, rows, natural_keys))
    log.info("load_static_data %s from %s: %s", table.name, csv_path, result)
    return result
