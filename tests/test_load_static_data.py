from __future__ import annotations

import datetime

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.runtime import plugins

from alembic_static_data.errors import ColumnMismatchError
from alembic_static_data.errors import CsvFormatError
from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.errors import OfflineModeError
from alembic_static_data.errors import StaticDataError
from alembic_static_data.errors import ValueCoercionError
from alembic_static_data.upsert import UpsertResult
from conftest import fetch_all

BASE = "code,name,decimals\nEUR,Euro,2\nJPY,Yen,0\n"


def test_plugin_is_discovered_through_entry_point_and_registers_operation() -> None:
    assert "alembic_static_data" in plugins._all_plugins
    assert hasattr(Operations, "load_static_data")


def test_inserts_new_rows(op, connection, write_csv) -> None:
    result = op.load_static_data("currency", write_csv(BASE), ["code"])

    assert result == UpsertResult(inserted=2, updated=0, unchanged=0)
    assert [(r["code"], r["name"], r["decimals"]) for r in fetch_all(connection)] == [
        ("EUR", "Euro", 2),
        ("JPY", "Yen", 0),
    ]


def test_rerun_is_idempotent(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"])
    before = fetch_all(connection)

    result = op.load_static_data("currency", csv_path, ["code"])

    assert result == UpsertResult(inserted=0, updated=0, unchanged=2)
    assert fetch_all(connection) == before


def test_changed_value_is_updated_and_new_row_inserted(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    result = op.load_static_data(
        "currency", write_csv("code,name,decimals\nEUR,Euro (new name),2\nJPY,Yen,0\nCHF,Franc,2\n"), ["code"]
    )

    assert result == UpsertResult(inserted=1, updated=1, unchanged=1)
    names = {r["code"]: r["name"] for r in fetch_all(connection)}
    assert names == {"CHF": "Franc", "EUR": "Euro (new name)", "JPY": "Yen"}


def test_row_removed_from_csv_is_not_deleted(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    op.load_static_data("currency", write_csv("code,name,decimals\nEUR,Euro,2\n"), ["code"])

    assert [r["code"] for r in fetch_all(connection)] == ["EUR", "JPY"]


def test_columns_missing_from_csv_are_left_untouched(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])
    connection.execute(sa.text("UPDATE currency SET decimals = 9 WHERE code = 'EUR'"))

    op.load_static_data("currency", write_csv("code,name\nEUR,Euro\n"), ["code"])

    assert {r["code"]: r["decimals"] for r in fetch_all(connection)}["EUR"] == 9


def test_cells_are_coerced_to_column_types_and_empty_becomes_null(op, connection, write_csv) -> None:
    csv_path = write_csv("code,name,decimals,active,introduced\nEUR,Euro,2,true,1999-01-01\nXXX,Test,,no,\n")

    op.load_static_data("currency", csv_path, ["code"])

    rows = {r["code"]: r for r in fetch_all(connection)}
    assert rows["EUR"]["decimals"] == 2
    assert rows["EUR"]["active"] == 1
    assert rows["EUR"]["introduced"] == "1999-01-01"
    assert rows["XXX"]["decimals"] is None
    assert rows["XXX"]["active"] == 0
    assert rows["XXX"]["introduced"] is None


def test_date_values_roundtrip_through_a_date_column(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv("code,name,introduced\nEUR,Euro,1999-01-01\n"), ["code"])
    table = sa.Table("currency", sa.MetaData(), autoload_with=connection)
    assert connection.execute(sa.select(table.c.introduced)).scalar_one() == datetime.date(1999, 1, 1)


def test_cell_can_be_cleared_back_to_null(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    op.load_static_data("currency", write_csv("code,name,decimals\nEUR,Euro,\nJPY,Yen,0\n"), ["code"])

    assert {r["code"]: r["decimals"] for r in fetch_all(connection)}["EUR"] is None


def test_composite_natural_key(op, connection, write_csv) -> None:
    op.create_table(
        "rate",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("country", sa.String(2), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("pct", sa.Float, nullable=False),
    )
    keys = ["country", "kind"]
    op.load_static_data("rate", write_csv("country,kind,pct\nDE,vat,19\nDE,reduced,7\nFR,vat,20\n", "rate.csv"), keys)

    result = op.load_static_data(
        "rate", write_csv("country,kind,pct\nDE,vat,19\nDE,reduced,7\nFR,vat,20.5\n", "rate.csv"), keys
    )

    assert result == UpsertResult(inserted=0, updated=1, unchanged=2)
    rows = connection.execute(sa.text("SELECT country, kind, pct FROM rate ORDER BY id")).all()
    assert [tuple(r) for r in rows] == [("DE", "vat", 19.0), ("DE", "reduced", 7.0), ("FR", "vat", 20.5)]


def test_accepts_bom_blank_lines_and_padded_header(op, write_csv) -> None:
    csv_path = write_csv("﻿code , name\nEUR,Euro\n\n")

    result = op.load_static_data("currency", csv_path, ["code"])

    assert result.inserted == 1


# ---- negative paths ----


def test_natural_key_missing_from_csv_header_raises(op, write_csv) -> None:
    with pytest.raises(NaturalKeyError, match=r"Natural key column\(s\) \['iso'\] missing from CSV header"):
        op.load_static_data("currency", write_csv(BASE), ["iso"])


def test_empty_natural_keys_raises(op, write_csv) -> None:
    with pytest.raises(NaturalKeyError, match="at least one column"):
        op.load_static_data("currency", write_csv(BASE), [])


def test_natural_key_not_a_table_column_raises(op, write_csv) -> None:
    csv_path = write_csv("code,name,iso\nEUR,Euro,EUR\n")
    with pytest.raises(ColumnMismatchError, match=r"CSV column\(s\) \['iso'\] not found in table 'currency'"):
        op.load_static_data("currency", csv_path, ["iso"])


def test_header_column_unknown_to_table_raises_and_writes_nothing(op, connection, write_csv) -> None:
    with pytest.raises(ColumnMismatchError, match=r"CSV column\(s\) \['bogus'\] not found in table 'currency'"):
        op.load_static_data("currency", write_csv("code,name,bogus\nEUR,Euro,x\n"), ["code"])
    assert fetch_all(connection) == []


def test_row_with_wrong_cell_count_raises_with_line_number(op, write_csv) -> None:
    with pytest.raises(CsvFormatError, match=r"line 3: expected 3 cells, got 2"):
        op.load_static_data("currency", write_csv("code,name,decimals\nEUR,Euro,2\nJPY,Yen\n"), ["code"])


def test_duplicate_natural_key_in_csv_raises_before_any_write(op, connection, write_csv) -> None:
    with pytest.raises(NaturalKeyError, match="Duplicate natural key"):
        op.load_static_data("currency", write_csv("code,name\nEUR,Euro\nEUR,Euro again\n"), ["code"])
    assert fetch_all(connection) == []


def test_missing_csv_file_raises(op, tmp_path) -> None:
    with pytest.raises(CsvFormatError, match="CSV file not found"):
        op.load_static_data("currency", tmp_path / "nope.csv", ["code"])


def test_empty_csv_file_raises(op, write_csv) -> None:
    with pytest.raises(CsvFormatError, match="empty"):
        op.load_static_data("currency", write_csv(""), ["code"])


@pytest.mark.parametrize(
    ("header", "message"),
    [("code,,name", "blank column name"), ("code,name,name", r"duplicate columns: \['name'\]")],
)
def test_bad_header_raises(op, write_csv, header: str, message: str) -> None:
    with pytest.raises(CsvFormatError, match=message):
        op.load_static_data("currency", write_csv(header + "\n"), ["code"])


@pytest.mark.parametrize(
    ("csv_text", "message"),
    [
        ("code,name,decimals\nEUR,Euro,two\n", "Cannot convert 'two' to int for column currency.decimals"),
        ("code,name,active\nEUR,Euro,maybe\n", "Cannot convert 'maybe' to bool"),
        ("code,name,introduced\nEUR,Euro,31.12.1999\n", "to date"),
    ],
)
def test_uncoercible_value_raises(op, write_csv, csv_text: str, message: str) -> None:
    with pytest.raises(ValueCoercionError, match=message):
        op.load_static_data("currency", write_csv(csv_text), ["code"])


def test_offline_mode_raises(tmp_path) -> None:
    context = MigrationContext.configure(dialect_name="sqlite", opts={"as_sql": True})
    with Operations.context(context) as operations, pytest.raises(OfflineModeError, match="offline mode"):
        operations.load_static_data("currency", tmp_path / "x.csv", ["code"])


def test_all_errors_share_a_base_class() -> None:
    for error in (CsvFormatError, NaturalKeyError, ColumnMismatchError, ValueCoercionError, OfflineModeError):
        assert issubclass(error, StaticDataError)
