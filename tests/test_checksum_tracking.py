"""Checksum tracking: a load is skipped while its CSV is unchanged (issue #1)."""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from freezegun import freeze_time

from alembic_static_data.errors import CsvFormatError
from alembic_static_data.tracking import DEFAULT_VERSION_TABLE
from alembic_static_data.tracking import checksum_for
from alembic_static_data.tracking import tracking_key
from alembic_static_data.tracking import version_table
from alembic_static_data.upsert import UpsertResult
from conftest import fetch_all

BASE = "code,name,decimals\nEUR,Euro,2\nJPY,Yen,0\n"
CHANGED = "code,name,decimals\nEUR,Euro,2\nJPY,Yen,0\nCHF,Franc,2\n"


def versions(connection: sa.Connection, table: str = DEFAULT_VERSION_TABLE) -> list[dict[str, object]]:
    versions_table = version_table(table)
    rows = connection.execute(
        sa.select(versions_table).order_by(versions_table.c.table_name, versions_table.c.csv_path)
    )
    return [dict(row) for row in rows.mappings()]


# ---- skipping ----


def test_unchanged_csv_is_skipped_on_rerun(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    first = op.load_static_data("currency", csv_path, ["code"])

    second = op.load_static_data("currency", csv_path, ["code"])

    assert first == UpsertResult(inserted=2)
    assert second == UpsertResult(skipped=True)
    assert second.rows == 0  # nothing was read
    assert [(r["code"], r["name"]) for r in fetch_all(connection)] == [("EUR", "Euro"), ("JPY", "Yen")]


def test_skip_does_not_notice_drift_in_the_table(op, connection, write_csv) -> None:
    # The checksum describes the file, not the table: a hand-edited row stays edited until the CSV changes.
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"])
    connection.execute(sa.text("UPDATE currency SET name = 'hand edited' WHERE code = 'EUR'"))

    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(skipped=True)
    assert {r["code"]: r["name"] for r in fetch_all(connection)}["EUR"] == "hand edited"


def test_changed_csv_runs_again_and_updates_the_recorded_checksum(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])
    before = versions(connection)[0]["checksum"]

    result = op.load_static_data("currency", write_csv(CHANGED), ["code"])

    assert result == UpsertResult(inserted=1, unchanged=2)
    recorded = versions(connection)
    assert len(recorded) == 1, "the same (table, csv) pair must stay one row"
    assert recorded[0]["checksum"] != before
    assert recorded[0]["checksum"] == checksum_for(write_csv(CHANGED))
    assert recorded[0]["rows"] == 3


def test_reverting_the_csv_runs_the_load_again(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])
    op.load_static_data("currency", write_csv(CHANGED), ["code"])

    # Back to the original content: the stored checksum is the changed one, so this is a change again.
    assert op.load_static_data("currency", write_csv(BASE), ["code"]) == UpsertResult(unchanged=2)


def test_tracking_can_be_switched_off(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"], track_checksum=False)

    result = op.load_static_data("currency", csv_path, ["code"], track_checksum=False)

    assert result == UpsertResult(unchanged=2), "without tracking, the upsert runs and finds nothing to do"
    with pytest.raises(sa.exc.OperationalError):  # no checksum table was created
        versions(connection)


# ---- the checksum table itself ----


def test_checksum_table_records_one_row_per_table_and_file(op, connection, write_csv) -> None:
    op.create_table(
        "rate",
        sa.Column("country", sa.String(2), primary_key=True),
        sa.Column("pct", sa.Float, nullable=False),
    )
    currency_csv = write_csv(BASE)
    rate_csv = write_csv("country,pct\nDE,19\n", "rate.csv")

    op.load_static_data("currency", currency_csv, ["code"])
    op.load_static_data("rate", rate_csv, ["country"])

    recorded = {(r["table_name"], r["csv_path"]): r for r in versions(connection)}
    assert set(recorded) == {("currency", tracking_key(currency_csv)), ("rate", tracking_key(rate_csv))}
    assert recorded[("rate", tracking_key(rate_csv))]["rows"] == 1


def test_the_same_file_loaded_into_two_tables_is_tracked_separately(op, connection, write_csv) -> None:
    op.create_table(
        "currency_archive",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("decimals", sa.Integer, nullable=True),
    )
    csv_path = write_csv(BASE)

    first = op.load_static_data("currency", csv_path, ["code"])
    second = op.load_static_data("currency_archive", csv_path, ["code"])

    assert first == second == UpsertResult(inserted=2)
    assert len(versions(connection)) == 2


def test_version_table_name_is_configurable(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)

    op.load_static_data("currency", csv_path, ["code"], version_table_name="my_static_data")

    assert len(versions(connection, "my_static_data")) == 1
    # The default table was never created, and a load tracked elsewhere is not skipped by this one.
    assert op.load_static_data("currency", csv_path, ["code"]) == UpsertResult(unchanged=2)


def test_checksum_is_the_sha256_of_the_file_bytes(write_csv) -> None:
    csv_path = write_csv(BASE)
    # Deliberately hashed from disk rather than from BASE: on Windows the fixture writes CRLF line endings,
    # and the contract is the bytes of the file, not the logical text.
    assert checksum_for(csv_path) == hashlib.sha256(Path(csv_path).read_bytes()).hexdigest()


def test_checksum_of_a_missing_file_raises_before_any_table_is_created(op, connection, tmp_path) -> None:
    with pytest.raises(CsvFormatError, match="CSV file not found"):
        op.load_static_data("currency", tmp_path / "nope.csv", ["code"])
    with pytest.raises(sa.exc.OperationalError):
        versions(connection)


def test_missing_csv_file_raises_without_tracking(op, tmp_path) -> None:
    # Without the checksum pre-check, the CSV reader itself reports the missing file.
    with pytest.raises(CsvFormatError, match="CSV file not found"):
        op.load_static_data("currency", tmp_path / "nope.csv", ["code"], track_checksum=False)


def test_applied_at_is_recorded_in_utc(op, connection, write_csv) -> None:
    with freeze_time("2026-10-22 08:30:00"):
        op.load_static_data("currency", write_csv(BASE), ["code"])

    applied_at = versions(connection)[0]["applied_at"]
    assert applied_at == datetime.datetime(2026, 10, 22, 8, 30)


def test_a_rewritten_but_equivalent_file_still_counts_as_changed(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    # Same rows, different bytes: hashing bytes is the cheap rule, so the load runs and finds nothing to do.
    result = op.load_static_data("currency", write_csv(BASE.replace(",2\n", ", 2\n")), ["code"])

    assert result == UpsertResult(unchanged=2)


# ---- opt-in deletion ----


def test_delete_missing_removes_rows_absent_from_the_csv(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    result = op.load_static_data(
        "currency", write_csv("code,name,decimals\nEUR,Euro,2\n"), ["code"], delete_missing=True
    )

    assert result == UpsertResult(unchanged=1, deleted=1)
    assert [r["code"] for r in fetch_all(connection)] == ["EUR"]


def test_delete_missing_is_off_by_default(op, connection, write_csv) -> None:
    op.load_static_data("currency", write_csv(BASE), ["code"])

    op.load_static_data("currency", write_csv("code,name,decimals\nEUR,Euro,2\n"), ["code"])

    assert [r["code"] for r in fetch_all(connection)] == ["EUR", "JPY"]


def test_delete_missing_with_a_composite_key_deletes_only_the_absent_row(op, connection, write_csv) -> None:
    op.create_table(
        "rate",
        sa.Column("country", sa.String(2), primary_key=True),
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("pct", sa.Float, nullable=False),
    )
    keys = ["country", "kind"]
    op.load_static_data("rate", write_csv("country,kind,pct\nDE,vat,19\nDE,reduced,7\nFR,vat,20\n", "r.csv"), keys)

    result = op.load_static_data(
        "rate", write_csv("country,kind,pct\nDE,vat,19\nFR,vat,20\n", "r2.csv"), keys, delete_missing=True
    )

    assert result == UpsertResult(unchanged=2, deleted=1)
    rows = connection.execute(sa.text("SELECT country, kind FROM rate ORDER BY country, kind")).all()
    assert [tuple(r) for r in rows] == [("DE", "vat"), ("FR", "vat")]


def test_delete_missing_on_a_skipped_load_does_nothing(op, connection, write_csv) -> None:
    csv_path = write_csv(BASE)
    op.load_static_data("currency", csv_path, ["code"], delete_missing=True)
    connection.execute(sa.text("INSERT INTO currency (code, name) VALUES ('XXX', 'Added by hand')"))

    # The file is unchanged, so the load is skipped and the extra row survives.
    assert op.load_static_data("currency", csv_path, ["code"], delete_missing=True) == UpsertResult(skipped=True)
    assert [r["code"] for r in fetch_all(connection)] == ["EUR", "JPY", "XXX"]
