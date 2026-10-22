"""The natural keys must identify at most one row, and the table has to guarantee it (issue #11).

Without that guarantee the upsert's ``UPDATE ... WHERE <natural keys>`` hits every matching row while the
result counts one, which is silent data loss. These tests pin both the refusal and the escape hatch.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from alembic_static_data.errors import NaturalKeyError
from alembic_static_data.upsert import UpsertResult
from alembic_static_data.upsert import check_natural_keys_unique
from alembic_static_data.upsert import unique_key_sets
from conftest import fetch_all


def table_with(*items: sa.schema.SchemaItem, name: str = "t") -> sa.Table:
    """A standalone table, so the rule can be tested without a database."""
    return sa.Table(name, sa.MetaData(), *items)


# ---- the rule, on constructed tables ----


def test_primary_key_covers_the_natural_keys() -> None:
    table = table_with(sa.Column("code", sa.String, primary_key=True), sa.Column("name", sa.String))
    check_natural_keys_unique(table, ["code"])


def test_unique_constraint_covers_the_natural_keys() -> None:
    table = table_with(
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        sa.UniqueConstraint("code"),
    )
    check_natural_keys_unique(table, ["code"])


def test_unique_index_covers_the_natural_keys() -> None:
    table = table_with(sa.Column("id", sa.Integer, primary_key=True), sa.Column("code", sa.String))
    sa.Index("ix_code", table.c.code, unique=True)
    check_natural_keys_unique(table, ["code"])


def test_a_unique_subset_is_enough() -> None:
    # code alone is unique, so (code, name) matches at most one row too.
    table = table_with(
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        sa.Column("name", sa.String),
        sa.UniqueConstraint("code"),
    )
    check_natural_keys_unique(table, ["code", "name"])


def test_a_unique_superset_is_not_enough() -> None:
    # Unique on (code, name) says nothing about code alone: two rows may share it.
    table = table_with(
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        sa.Column("name", sa.String),
        sa.UniqueConstraint("code", "name"),
    )
    with pytest.raises(NaturalKeyError, match=r"not covered by a unique constraint"):
        check_natural_keys_unique(table, ["code"])


def test_no_unique_key_at_all_is_reported_with_what_was_found() -> None:
    table = table_with(
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        name="currency",
    )
    with pytest.raises(NaturalKeyError) as caught:
        check_natural_keys_unique(table, ["code"])

    message = str(caught.value)
    assert "natural_keys ['code']" in message
    assert "primary key: ['id']" in message
    assert "unique: none" in message
    assert "require_unique=False" in message


def test_composite_primary_key_is_covered() -> None:
    table = table_with(
        sa.Column("country", sa.String, primary_key=True),
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("pct", sa.Float),
    )
    check_natural_keys_unique(table, ["country", "kind"])


def test_unique_key_sets_lists_all_three_sources() -> None:
    table = table_with(
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String),
        sa.Column("iso", sa.String),
        sa.UniqueConstraint("code"),
    )
    sa.Index("ix_iso", table.c.iso, unique=True)

    assert unique_key_sets(table) == [["id"], ["code"], ["iso"]]


def test_a_table_without_any_key_reports_no_primary_key() -> None:
    table = table_with(sa.Column("code", sa.String), sa.Column("name", sa.String))
    with pytest.raises(NaturalKeyError, match="primary key: none"):
        check_natural_keys_unique(table, ["code"])


# ---- through the operation ----


def test_load_refuses_before_writing_anything(op, connection, write_csv) -> None:
    op.create_table(
        "loose",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(3)),
        sa.Column("name", sa.String(50)),
    )
    connection.execute(sa.text("INSERT INTO loose (code, name) VALUES ('EUR','Euro A'), ('EUR','Euro B')"))

    with pytest.raises(NaturalKeyError, match=r"not covered by a unique constraint on 'loose'"):
        op.load_static_data("loose", write_csv("code,name\nEUR,Euro NEU\n"), ["code"])

    rows = connection.execute(sa.text("SELECT name FROM loose ORDER BY id")).all()
    assert [row[0] for row in rows] == ["Euro A", "Euro B"], "nothing may have been written"


def test_require_unique_false_restores_the_old_unchecked_behaviour(op, connection, write_csv) -> None:
    """The escape hatch, and the reason it is one: one CSV row overwrites two table rows."""
    op.create_table(
        "loose",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(3)),
        sa.Column("name", sa.String(50)),
    )
    connection.execute(sa.text("INSERT INTO loose (code, name) VALUES ('EUR','Euro A'), ('EUR','Euro B')"))

    result = op.load_static_data("loose", write_csv("code,name\nEUR,Euro NEU\n"), ["code"], require_unique=False)

    assert result == UpsertResult(updated=1)
    rows = connection.execute(sa.text("SELECT name FROM loose ORDER BY id")).all()
    assert [row[0] for row in rows] == ["Euro NEU", "Euro NEU"], "two rows changed, one was reported"


def test_a_unique_constraint_makes_the_load_work(op, connection, write_csv) -> None:
    op.create_table(
        "tight",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(3)),
        sa.Column("name", sa.String(50)),
        sa.UniqueConstraint("code"),
    )

    result = op.load_static_data("tight", write_csv("code,name\nEUR,Euro\n"), ["code"])

    assert result == UpsertResult(inserted=1)


def test_the_check_runs_without_checksum_tracking_too(op, write_csv) -> None:
    op.create_table("loose", sa.Column("id", sa.Integer, primary_key=True), sa.Column("code", sa.String(3)))

    with pytest.raises(NaturalKeyError, match="not covered by a unique constraint"):
        op.load_static_data("loose", write_csv("code\nEUR\n"), ["code"], track_checksum=False)


# ---- a single natural key may be a plain string ----


def test_a_string_natural_key_is_treated_as_one_column(op, connection, write_csv) -> None:
    # Without normalising, a string iterates into characters and reports ['c', 'o', 'd', 'e'].
    result = op.load_static_data("currency", write_csv("code,name\nEUR,Euro\n"), "code")

    assert result == UpsertResult(inserted=1)
    assert [row["code"] for row in fetch_all(connection)] == ["EUR"]


def test_the_string_and_list_forms_behave_identically(op, connection, write_csv) -> None:
    csv_path = write_csv("code,name\nEUR,Euro\n")
    op.load_static_data("currency", csv_path, "code", track_checksum=False)

    again = op.load_static_data("currency", csv_path, ["code"], track_checksum=False)

    assert again == UpsertResult(unchanged=1)
