"""The same behaviour against a real Postgres, skipped unless Docker is available.

Everything else in the suite runs on in-memory SQLite, which cannot exercise named schemas, server-side
numeric types, or a dialect where DDL is transactional. These tests cover that gap. They are skipped (not
failed) when Docker is missing, which is the case on the current development machine.
"""

from __future__ import annotations

import datetime
import decimal
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import tracking_key
from alembic_static_data.tracking import version_table
from alembic_static_data.upsert import UpsertResult

postgres = pytest.importorskip("testcontainers.postgres", reason="testcontainers is not installed")
pytest.importorskip("psycopg", reason="the psycopg driver is not installed")

# Pulling and starting the image can take far longer than the suite-wide 60s timeout.
pytestmark = pytest.mark.timeout(600)

BASE = "code,name,decimals\nEUR,Euro,2\nJPY,Yen,0\n"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A throwaway Postgres, or a skip when Docker is not reachable."""
    try:
        container = postgres.PostgresContainer("postgres:17-alpine", driver="psycopg")
        container.start()
    except Exception as exc:  # docker missing, not running, or image unavailable
        pytest.skip(f"Docker is not available: {type(exc).__name__}: {exc}")
    try:
        yield container.get_connection_url()
    finally:
        container.stop()


@pytest.fixture
def connection(postgres_url: str) -> Iterator[sa.Connection]:
    """A connection in a transaction that is rolled back, so each test starts from an empty database.

    Postgres DDL is transactional, so the tables created below disappear with the rollback too.
    """
    engine = sa.create_engine(postgres_url)
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()
    engine.dispose()


@pytest.fixture
def op(connection: sa.Connection) -> Iterator[Operations]:
    context = MigrationContext.configure(connection)
    with Operations.context(context) as operations:
        operations.create_table(
            "currency",
            sa.Column("code", sa.String(3), primary_key=True),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("decimals", sa.Integer, nullable=True),
            sa.Column("active", sa.Boolean, nullable=True),
            sa.Column("introduced", sa.Date, nullable=True),
            sa.Column("rate", sa.Numeric(12, 4), nullable=True),
        )
        yield operations


@pytest.fixture
def write_csv(tmp_path: Path) -> Callable[..., Path]:
    def _write(content: str, name: str = "currency.csv") -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


def currencies(connection: sa.Connection) -> list[dict[str, object]]:
    rows = connection.execute(sa.text("SELECT * FROM currency ORDER BY code"))
    return [dict(row) for row in rows.mappings()]


def test_insert_then_skip_then_update(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)

    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(inserted=2)
    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(skipped=True)

    changed = write_csv("code,name,decimals\nEUR,Euro (renamed),2\nJPY,Yen,0\n")
    assert op.load_static_data("currency", changed, ["code"]) == UpsertResult(updated=1, unchanged=1)
    assert {row["code"]: row["name"] for row in currencies(connection)}["EUR"] == "Euro (renamed)"


def test_server_side_types_round_trip(op, connection, write_csv) -> None:
    csv_path = write_csv(
        "code,name,decimals,active,introduced,rate\nEUR,Euro,2,true,1999-01-01,1.2345\nXXX,Test,,no,,\n"
    )

    op.load_static_data("currency", csv_path, ["code"])

    rows = {row["code"]: row for row in currencies(connection)}
    assert rows["EUR"]["decimals"] == 2
    assert rows["EUR"]["active"] is True
    assert rows["EUR"]["introduced"] == datetime.date(1999, 1, 1)
    assert rows["EUR"]["rate"] == decimal.Decimal("1.2345")
    assert rows["XXX"]["active"] is False
    assert rows["XXX"]["introduced"] is None
    assert rows["XXX"]["rate"] is None


def test_rerun_with_identical_values_reports_unchanged_not_updated(op, write_csv) -> None:
    # Guards the comparison in changed_columns() against Postgres returning Decimal where the CSV gave a str.
    op.load_static_data("currency", write_csv("code,name,rate\nEUR,Euro,1.2345\n"), ["code"])

    result = op.load_static_data(
        "currency", write_csv("code,name,rate\nEUR,Euro,1.2345\n"), ["code"], track_checksum=False
    )

    assert result == UpsertResult(unchanged=1)


def test_delete_missing_removes_rows(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    result = op.load_static_data(
        "currency", write_csv("code,name,decimals\nEUR,Euro,2\n"), ["code"], delete_missing=True
    )

    assert result == UpsertResult(unchanged=1, deleted=1)
    assert [row["code"] for row in currencies(connection)] == ["EUR"]


def test_checksum_table_is_created_in_the_given_schema(op, connection, write_csv) -> None:
    # The schema= argument cannot be exercised on SQLite at all.
    connection.execute(sa.schema.CreateSchema("reference"))
    op.create_table(
        "country",
        sa.Column("iso", sa.String(2), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        schema="reference",
    )
    csv_path = write_csv("iso,name\nDE,Germany\nFR,France\n", "country.csv")

    first = op.load_static_data("country", csv_path, ["iso"], schema="reference")
    second = op.load_static_data("country", csv_path, ["iso"], schema="reference")

    assert first == UpsertResult(inserted=2)
    assert second == UpsertResult(skipped=True)
    versions = version_table(DEFAULT_VERSION_TABLE, "reference")
    recorded = connection.execute(sa.select(versions.c.table_name, versions.c.csv_path, versions.c.rows)).all()
    assert [tuple(row) for row in recorded] == [("country", tracking_key(csv_path), 2)]
