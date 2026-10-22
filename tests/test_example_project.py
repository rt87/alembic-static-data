"""Runs examples/reference_data through the real Alembic machinery.

Every other test drives ``MigrationContext``/``Operations`` directly. This one goes through
``alembic.command`` with a real ``alembic.ini``, a real ``env.py`` and real revision files, which is the only
place the following are exercised the way a user meets them:

* entry-point discovery (or ``register_operations()``) inside an ``env.py``,
* ``op.load_static_data()`` called from an actual revision, inside Alembic's transaction,
* ``op.forget_static_data()`` in a ``downgrade()``,
* ``include_name=exclude_from_autogenerate`` against a real autogenerate run,
* CSV paths resolved relative to the migration file rather than the working directory.

The example is copied into ``tmp_path`` so a run never writes into the repository.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "reference_data"


@pytest.fixture
def example(tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """A copy of the example project in tmp_path, plus its database URL."""
    project = tmp_path / "reference_data"
    shutil.copytree(EXAMPLE, project)
    url = f"sqlite:///{(project / 'example.db').as_posix()}"
    config = Config(str(project / "alembic.ini"))
    config.set_main_option("script_location", str(project / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    yield config, url


def rows(url: str, statement: str = "SELECT code, name, decimals FROM currency ORDER BY code") -> list[tuple[Any, ...]]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(sa.text(statement))]
    finally:
        engine.dispose()


def table_names(url: str) -> set[str]:
    engine = sa.create_engine(url)
    try:
        with engine.connect() as connection:
            return set(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_creates_the_table_and_loads_the_csv(example) -> None:
    config, url = example

    command.upgrade(config, "head")

    assert rows(url) == [
        ("CHF", "Swiss Franc", 2),
        ("EUR", "Euro", 2),
        ("JPY", "Yen", 0),
        ("XBT", "Bitcoin", 8),
    ]
    assert {"currency", "alembic_version", "static_data_version"} <= table_names(url)


def test_upgrading_again_from_a_different_working_directory_is_a_no_op(example, tmp_path, monkeypatch) -> None:
    config, url = example
    command.upgrade(config, "head")
    before = rows(url)

    # Stamp back one revision and re-run 0002 from an unrelated cwd: the CSV path must still resolve, and the
    # unchanged checksum must make the load a no-op.
    command.stamp(config, "0001")
    monkeypatch.chdir(tmp_path)
    command.upgrade(config, "head")

    assert rows(url) == before


def test_editing_the_csv_makes_the_next_run_reload_it(example) -> None:
    config, url = example
    command.upgrade(config, "head")

    csv_path = Path(config.get_main_option("script_location")).parent / "data" / "currency.csv"
    csv_path.write_text(
        csv_path.read_text(encoding="utf-8").replace("EUR,Euro,2", "EUR,Euro (edited),2"), encoding="utf-8"
    )
    command.stamp(config, "0001")
    command.upgrade(config, "head")

    assert ("EUR", "Euro (edited)", 2) in rows(url)


def test_full_downgrade_then_upgrade_brings_the_data_back(example) -> None:
    """The cycle op.forget_static_data() exists for, driven end to end by the CLI commands."""
    config, url = example
    command.upgrade(config, "head")

    command.downgrade(config, "base")
    assert "currency" not in table_names(url)

    command.upgrade(config, "head")
    assert len(rows(url)) == 4, "without forget_static_data in downgrade(), this table would be empty"


def test_autogenerate_does_not_propose_dropping_the_bookkeeping_table(example, tmp_path) -> None:
    config, _ = example
    command.upgrade(config, "head")

    # A real autogenerate run against the example's own target_metadata.
    command.revision(config, message="check", autogenerate=True, rev_id="0003")

    versions = Path(config.get_main_option("script_location")) / "versions"
    generated = next(path for path in versions.iterdir() if path.name.startswith("0003"))
    body = generated.read_text(encoding="utf-8")
    assert "static_data_version" not in body, body
    assert "drop_table" not in body, body


def test_offline_mode_fails_loudly_rather_than_emitting_wrong_sql(example) -> None:
    config, _ = example
    # Offline mode cannot read the database, so the load has nothing to decide from; env.py documents this.
    with pytest.raises(Exception, match="offline mode"):
        command.upgrade(config, "head", sql=True)
