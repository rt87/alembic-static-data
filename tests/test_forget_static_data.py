"""``op.forget_static_data()``: the inverse of the load's bookkeeping (issue #4)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from alembic_static_data.errors import OfflineModeError
from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import version_table
from alembic_static_data.upsert import UpsertResult
from conftest import fetch_all

BASE = "code,name,decimals\nEUR,Euro,2\nJPY,Yen,0\n"


def recorded(connection: sa.Connection, table: str = DEFAULT_VERSION_TABLE) -> list[tuple[object, ...]]:
    versions = version_table(table)
    rows = connection.execute(
        sa.select(versions.c.table_name, versions.c.csv_path).order_by(versions.c.table_name, versions.c.csv_path)
    )
    return [tuple(row) for row in rows]


def test_the_upgrade_downgrade_upgrade_cycle_reloads(op, connection, write_csv) -> None:
    """The bug this operation exists for: without the forget, the second upgrade skips and the data is lost."""
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"])

    # What a downgrade() does: drop the data, then forget that the file was loaded.
    connection.execute(sa.text("DELETE FROM currency"))
    forgotten = op.forget_static_data("currency", csv_path)

    assert forgotten == 1
    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(inserted=2)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY"]


def test_without_forgetting_the_reload_is_skipped(op, connection, write_csv) -> None:
    # The same cycle without the new operation, pinning why it is needed.
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"])
    connection.execute(sa.text("DELETE FROM currency"))

    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(skipped=True)
    assert fetch_all(connection) == []


def test_forgets_only_the_named_file(op, connection, write_csv) -> None:
    first = write_csv("code,name\nEUR,Euro\n", "first.csv")
    second = write_csv("code,name\nJPY,Yen\n", "second.csv")
    op.load_static_data("currency", first, ["code"])
    op.load_static_data("currency", second, ["code"])

    assert op.forget_static_data("currency", first) == 1

    assert [table for table, _ in recorded(connection)] == ["currency"]
    assert op.load_static_data("currency", second, ["code"]) == UpsertResult(skipped=True)


def test_without_a_csv_path_forgets_every_file_for_that_table(op, connection, write_csv) -> None:
    op.create_table("rate", sa.Column("country", sa.String(2), primary_key=True))
    op.load_static_data("currency", write_csv("code,name\nEUR,Euro\n", "a.csv"), ["code"])
    op.load_static_data("currency", write_csv("code,name\nJPY,Yen\n", "b.csv"), ["code"])
    op.load_static_data("rate", write_csv("country\nDE\n", "rate.csv"), ["country"])

    assert op.forget_static_data("currency") == 2

    assert [table for table, _ in recorded(connection)] == ["rate"], "other tables are untouched"


def test_forgetting_nothing_is_not_an_error(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    assert op.forget_static_data("currency", "never/loaded.csv") == 0
    assert op.forget_static_data("some_other_table") == 0
    assert len(recorded(connection)) == 1


def test_does_not_create_the_checksum_table(op, connection) -> None:
    # A downgrade must never add a table, and there is nothing recorded to delete anyway.
    assert op.forget_static_data("currency", "data/currency.csv") == 0
    assert not sa.inspect(connection).has_table(DEFAULT_VERSION_TABLE)


def test_respects_a_renamed_checksum_table(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"], version_table_name="my_static_data")

    assert op.forget_static_data("currency", csv_path) == 0, "the default table holds nothing"
    assert op.forget_static_data("currency", csv_path, version_table_name="my_static_data") == 1
    assert recorded(connection, "my_static_data") == []


def test_forgetting_leaves_table_data_alone(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"])

    op.forget_static_data("currency", csv_path)

    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY"]


def test_offline_mode_raises() -> None:
    context = MigrationContext.configure(dialect_name="sqlite", opts={"as_sql": True})
    with Operations.context(context) as operations, pytest.raises(OfflineModeError, match="offline mode"):
        # Alembic registers the operation at runtime, so it is not an attribute mypy can see.
        operations.forget_static_data("currency", "data/currency.csv")  # type: ignore[attr-defined]
