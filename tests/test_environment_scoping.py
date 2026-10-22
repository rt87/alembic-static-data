"""Environment scoping: one CSV, a column saying which environments each row belongs to (issue #9).

The environment itself comes from Alembic's ``-x`` argument, which only exists when an ``EnvironmentContext``
is in play. The ``op_in_environment`` fixture builds a real one, so these tests go through the same code path
as ``alembic -x env=prod upgrade head`` rather than around it.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory

from alembic_static_data.errors import CsvFormatError
from alembic_static_data.errors import EnvironmentNotSetError
from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import version_table
from alembic_static_data.upsert import UpsertResult
from conftest import fetch_all

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "reference_data"

# EUR/JPY apply everywhere, XTS only to dev, XXX to dev and staging.
SCOPED = (
    "code,name,decimals,_env\n" "EUR,Euro,2,\n" "JPY,Yen,0,\n" "XTS,Test currency,2,dev\n" "XXX,Sandbox,2,dev;staging\n"
)


@pytest.fixture
def op_in_environment(connection: sa.Connection) -> Callable[..., Operations]:
    """Builds an Operations bound to a MigrationContext that carries ``-x`` arguments."""

    def _build(*x_arguments: str) -> Operations:
        config = Config(str(EXAMPLE / "alembic.ini"))
        config.set_main_option("script_location", str(EXAMPLE / "migrations"))
        config.cmd_opts = argparse.Namespace(x=list(x_arguments))
        environment_context = EnvironmentContext(config, ScriptDirectory.from_config(config))
        context = MigrationContext.configure(connection, environment_context=environment_context)
        operations = Operations(context)
        operations.create_table(
            "currency",
            sa.Column("code", sa.String(3), primary_key=True),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("decimals", sa.Integer, nullable=True),
        )
        return operations

    return _build


def recorded(connection: sa.Connection) -> list[tuple[object, ...]]:
    versions = version_table(DEFAULT_VERSION_TABLE)
    rows = connection.execute(
        sa.select(versions.c.table_name, versions.c.environment, versions.c.rows).order_by(versions.c.environment)
    )
    return [tuple(row) for row in rows]


# ---- selecting rows ----


def test_production_gets_only_the_unscoped_rows(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=prod")

    result = op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")

    assert result == UpsertResult(inserted=2)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY"]


def test_dev_gets_everything_that_mentions_it(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=dev")

    result = op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")

    assert result == UpsertResult(inserted=4)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY", "XTS", "XXX"]


def test_a_semicolon_list_matches_each_of_its_entries(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=staging")

    op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")

    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY", "XXX"]


def test_environment_names_are_compared_case_insensitively(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=DEV")

    op.load_static_data("currency", write_csv("code,name,_env\nXTS,Test,  dev \n"), ["code"], environment_column="_env")

    assert [row["code"] for row in fetch_all(connection)] == ["XTS"]


def test_an_unknown_environment_still_loads_the_unscoped_rows(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=qa")

    result = op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")

    assert result == UpsertResult(inserted=2)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY"]


def test_the_environment_column_is_not_written_to_the_table(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=dev")

    op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")

    # It is bookkeeping, not data: the table has no such column and the load must not try to write it.
    assert "_env" not in {column["name"] for column in sa.inspect(connection).get_columns("currency")}


def test_the_same_natural_key_can_appear_once_per_environment(op_in_environment, connection, write_csv) -> None:
    """The override pattern: a row that differs in dev. Without scoping this file is a duplicate-key error."""
    csv = "code,name,decimals,_env\nEUR,Euro,2,prod\nEUR,Euro (dev fixture),2,dev\n"

    op = op_in_environment("env=dev")
    op.load_static_data("currency", write_csv(csv), ["code"], environment_column="_env")

    assert {row["code"]: row["name"] for row in fetch_all(connection)} == {"EUR": "Euro (dev fixture)"}


def test_two_rows_for_the_selected_environment_are_still_a_duplicate(op_in_environment, write_csv) -> None:
    csv = "code,name,_env\nEUR,Euro,dev\nEUR,Euro again,dev\n"
    op = op_in_environment("env=dev")

    with pytest.raises(NaturalKeyError, match="Duplicate natural key"):
        op.load_static_data("currency", write_csv(csv), ["code"], environment_column="_env")


# ---- tracking per environment ----


def test_loading_a_second_environment_is_not_skipped(op_in_environment, connection, write_csv) -> None:
    """The reason the environment belongs in the tracking key: identical bytes, different rows."""
    csv_path = write_csv(SCOPED)
    prod = op_in_environment("env=prod")
    prod.load_static_data("currency", csv_path, ["code"], environment_column="_env")

    # Same file, same bytes, different environment: must run rather than skip.
    second = _load_as(connection, csv_path, "dev")

    assert second == UpsertResult(inserted=2, unchanged=2)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR", "JPY", "XTS", "XXX"]
    assert [(table, env) for table, env, _ in recorded(connection)] == [("currency", "dev"), ("currency", "prod")]


def test_rerunning_the_same_environment_is_skipped(op_in_environment, connection, write_csv) -> None:
    csv_path = write_csv(SCOPED)
    op = op_in_environment("env=prod")
    op.load_static_data("currency", csv_path, ["code"], environment_column="_env")

    again = op.load_static_data("currency", csv_path, ["code"], environment_column="_env")

    assert again == UpsertResult(skipped=True)


def test_forgetting_one_environment_leaves_the_other(op_in_environment, connection, write_csv) -> None:
    csv_path = write_csv(SCOPED)
    op = op_in_environment("env=prod")
    op.load_static_data("currency", csv_path, ["code"], environment_column="_env")
    _load_as(connection, csv_path, "dev")

    assert op.forget_static_data("currency", csv_path, environment="dev") == 1

    assert [env for _, env, _ in recorded(connection)] == ["prod"]


def test_forgetting_without_an_environment_forgets_all_of_them(op_in_environment, connection, write_csv) -> None:
    csv_path = write_csv(SCOPED)
    op = op_in_environment("env=prod")
    op.load_static_data("currency", csv_path, ["code"], environment_column="_env")
    _load_as(connection, csv_path, "dev")

    assert op.forget_static_data("currency", csv_path) == 2
    assert recorded(connection) == []


# ---- configuration errors ----


def test_without_an_x_argument_it_refuses_rather_than_loading_a_subset(op_in_environment, write_csv) -> None:
    op = op_in_environment()  # no -x at all

    with pytest.raises(EnvironmentNotSetError, match=r"alembic -x env=<name>"):
        op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")


def test_without_an_environment_context_it_also_refuses(op, write_csv) -> None:
    # The plain `op` fixture configures a MigrationContext directly, so there is no EnvironmentContext and
    # therefore no -x to read. Refusing beats loading an arbitrary subset.
    with pytest.raises(EnvironmentNotSetError, match="no environment was found"):
        op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env")


def test_the_x_argument_key_is_configurable(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("stage=dev")

    op.load_static_data("currency", write_csv(SCOPED), ["code"], environment_column="_env", environment_key="stage")

    assert len(fetch_all(connection)) == 4


def test_a_missing_environment_column_is_an_error(op_in_environment, write_csv) -> None:
    op = op_in_environment("env=dev")

    with pytest.raises(CsvFormatError, match=r"environment column '_env' not found in header"):
        op.load_static_data("currency", write_csv("code,name\nEUR,Euro\n"), ["code"], environment_column="_env")


def test_the_environment_column_cannot_be_a_natural_key(op_in_environment, write_csv) -> None:
    op = op_in_environment("env=dev")

    with pytest.raises(NaturalKeyError, match="cannot also be a natural key"):
        op.load_static_data("currency", write_csv(SCOPED), ["code", "_env"], environment_column="_env")


def test_scoping_works_without_checksum_tracking(op_in_environment, connection, write_csv) -> None:
    op = op_in_environment("env=prod")

    result = op.load_static_data(
        "currency", write_csv(SCOPED), ["code"], environment_column="_env", track_checksum=False
    )

    assert result == UpsertResult(inserted=2)
    assert not sa.inspect(connection).has_table(DEFAULT_VERSION_TABLE)


def _load_as(connection: sa.Connection, csv_path: Path, environment: str) -> UpsertResult:
    """Run a load as if `alembic -x env=<environment>` had been used, on the same connection."""
    config = Config(str(EXAMPLE / "alembic.ini"))
    config.set_main_option("script_location", str(EXAMPLE / "migrations"))
    config.cmd_opts = argparse.Namespace(x=[f"env={environment}"])
    context = MigrationContext.configure(
        connection, environment_context=EnvironmentContext(config, ScriptDirectory.from_config(config))
    )
    operations = Operations(context)
    # Alembic registers the operation at runtime, so it is not an attribute mypy can see.
    result = operations.load_static_data(  # type: ignore[attr-defined]
        "currency", csv_path, ["code"], environment_column="_env"
    )
    assert result is not None
    return cast("UpsertResult", result)
