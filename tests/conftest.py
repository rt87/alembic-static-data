from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from alembic_static_data import register_operations

# On Alembic < 1.18 nothing discovers the plugin, so register the operation the way such an env.py would.
register_operations()


@pytest.fixture
def connection() -> Iterator[sa.Connection]:
    engine = sa.create_engine("sqlite://", poolclass=sa.StaticPool)
    with engine.connect() as conn:
        yield conn
    engine.dispose()


@pytest.fixture
def op(connection: sa.Connection) -> Iterator[Operations]:
    """An Operations object bound to an in-memory SQLite database through a real MigrationContext."""
    context = MigrationContext.configure(connection)
    with Operations.context(context) as operations:
        operations.create_table(
            "currency",
            sa.Column("code", sa.String(3), primary_key=True),
            sa.Column("name", sa.String(50), nullable=False),
            sa.Column("decimals", sa.Integer, nullable=True),
            sa.Column("active", sa.Boolean, nullable=True),
            sa.Column("introduced", sa.Date, nullable=True),
        )
        yield operations


@pytest.fixture
def write_csv(tmp_path: Path) -> Callable[..., Path]:
    def _write(content: str, name: str = "currency.csv") -> Path:
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        return path

    return _write


def fetch_all(connection: sa.Connection, order_by: str = "code") -> list[dict[str, Any]]:
    result = connection.execute(sa.text(f"SELECT * FROM currency ORDER BY {order_by}"))
    return [dict(row) for row in result.mappings()]
