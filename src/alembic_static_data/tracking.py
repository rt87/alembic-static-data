"""Per-file checksum tracking, so a load can be skipped when its CSV has not changed.

The checksums live in their own table (``static_data_version`` by default), created on first use in the same
schema as the table being loaded, next to Alembic's own ``alembic_version``. A load either matches the stored
checksum and is skipped, or it runs and overwrites that one row; nothing here deletes history.
"""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import sqlalchemy as sa

from alembic_static_data.errors import CsvFormatError

DEFAULT_VERSION_TABLE = "static_data_version"

# MySQL caps a composite primary key at 3072 bytes, i.e. 768 utf8mb4 characters, so keep both key columns short.
_TABLE_NAME_LENGTH = 128
_CSV_PATH_LENGTH = 512


def checksum_for(csv_path: str | Path) -> str:
    """SHA-256 of the file's bytes, as hex.

    Hashes bytes rather than parsed rows on purpose: a reformatted file that parses to identical rows still
    counts as changed, which is the cheap and predictable rule. One consequence worth knowing: line endings
    are part of the bytes, so the same CSV checked out with CRLF on Windows and LF on POSIX hashes
    differently, and the first load on the other platform re-runs (harmlessly, as the upsert is idempotent).
    Raises ``CsvFormatError`` for a missing file, so a missing CSV fails the same way whether tracking is on
    or off.
    """
    path = Path(csv_path)
    if not path.is_file():
        raise CsvFormatError(f"CSV file not found: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tracking_key(csv_path: str | Path) -> str:
    """The stored form of ``csv_path``: separators normalised so Windows and POSIX agree on one key.

    The path is stored as written, not resolved, so a relative path in a migration script stays stable no
    matter where the repository is checked out.
    """
    return Path(csv_path).as_posix()


def version_table(name: str = DEFAULT_VERSION_TABLE, schema: str | None = None) -> sa.Table:
    """Definition of the checksum table. The ``MetaData`` is local: this table is never reflected."""
    return sa.Table(
        name,
        sa.MetaData(),
        sa.Column("table_name", sa.String(_TABLE_NAME_LENGTH), primary_key=True),
        sa.Column("csv_path", sa.String(_CSV_PATH_LENGTH), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("rows", sa.Integer, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=False),
        schema=schema,
    )


def ensure_version_table(connection: sa.Connection, versions: sa.Table) -> None:
    """Create the checksum table if it is not there yet."""
    versions.create(bind=connection, checkfirst=True)


def stored_checksum(connection: sa.Connection, versions: sa.Table, table_name: str, csv_key: str) -> str | None:
    """The checksum recorded for this ``(table, csv)`` pair, or ``None`` if it was never loaded."""
    stored = connection.execute(
        sa.select(versions.c.checksum).where(versions.c.table_name == table_name, versions.c.csv_path == csv_key)
    ).scalar_one_or_none()
    return stored


def record_load(
    connection: sa.Connection,
    versions: sa.Table,
    table_name: str,
    csv_key: str,
    checksum: str,
    rows: int,
    now: datetime.datetime | None = None,
) -> None:
    """Store (or replace) the checksum row for this ``(table, csv)`` pair."""
    applied_at = now if now is not None else datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    values = {"checksum": checksum, "rows": rows, "applied_at": applied_at}
    where = (versions.c.table_name == table_name, versions.c.csv_path == csv_key)
    if stored_checksum(connection, versions, table_name, csv_key) is None:
        connection.execute(sa.insert(versions).values(table_name=table_name, csv_path=csv_key, **values))
    else:
        connection.execute(sa.update(versions).where(*where).values(**values))
