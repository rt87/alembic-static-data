"""Small direct tests of the separately testable building blocks."""

from __future__ import annotations

import datetime
import decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.types import UserDefinedType

from alembic_static_data.errors import ColumnMismatchError
from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.upsert import build_insert
from alembic_static_data.upsert import build_select
from alembic_static_data.upsert import build_update
from alembic_static_data.upsert import changed_columns
from alembic_static_data.upsert import check_columns
from alembic_static_data.upsert import coerce_value

TABLE = sa.Table(
    "t",
    sa.MetaData(),
    sa.Column("k", sa.String, primary_key=True),
    sa.Column("v", sa.Integer),
    sa.Column("ts", sa.DateTime),
    sa.Column("amount", sa.Numeric),
    sa.Column("ratio", sa.Float),
)


class OpaqueType(UserDefinedType[Any]):
    cache_ok = True

    @property
    def python_type(self) -> type:
        raise NotImplementedError

    def get_col_spec(self, **kw: object) -> str:
        return "OPAQUE"


def test_coerce_value_covers_datetime_decimal_float_and_empty() -> None:
    assert coerce_value("2020-01-02T03:04:05", TABLE.c.ts) == datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert coerce_value("1.50", TABLE.c.amount) == decimal.Decimal("1.50")
    assert coerce_value("0.5", TABLE.c.ratio) == 0.5
    assert coerce_value("", TABLE.c.v) is None


def test_coerce_value_passes_strings_through_for_types_without_python_type() -> None:
    column = sa.Column("o", OpaqueType())
    sa.Table("u", sa.MetaData(), column)
    assert coerce_value("abc", column) == "abc"


def test_coerce_value_bool_variants() -> None:
    column = sa.Column("b", sa.Boolean)
    sa.Table("b", sa.MetaData(), column)
    assert [coerce_value(raw, column) for raw in ("TRUE", " y ", "0", "No")] == [True, True, False, False]


def test_build_statements_render_expected_sql() -> None:
    row = {"k": "a", "v": 1}
    assert "WHERE t.k = :k_1" in str(build_select(TABLE, ["k"], row))
    assert str(build_insert(TABLE, row)).startswith("INSERT INTO t (k, v)")
    update = str(build_update(TABLE, ["k"], row, {"v": 1}))
    assert update.startswith("UPDATE t SET v=:v") and "WHERE t.k = :k_1" in update


def test_changed_columns_ignores_keys_and_equal_values() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        existing = conn.execute(sa.select(sa.literal("a").label("k"), sa.literal(1).label("v"))).mappings().one()
    engine.dispose()
    assert changed_columns(existing, {"k": "other", "v": 1}, ["k"]) == {}
    assert changed_columns(existing, {"k": "a", "v": 2}, ["k"]) == {"v": 2}


def test_check_columns_accepts_known_columns() -> None:
    check_columns(TABLE, ["k", "v"], ["k"])


def test_check_columns_rejects_unknown_header_column() -> None:
    with pytest.raises(ColumnMismatchError, match="nope"):
        check_columns(TABLE, ["k", "nope"], ["k"])


def test_check_columns_rejects_natural_key_not_in_table() -> None:
    with pytest.raises(NaturalKeyError, match="not found in table 't'"):
        check_columns(TABLE, ["k"], ["missing"])
