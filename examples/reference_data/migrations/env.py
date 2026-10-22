"""Alembic environment for the worked example.

Shows the two things a real project has to wire up:

1. ``register_operations()`` — only needed on Alembic < 1.18, which has no plugin system. Harmless and
   idempotent on newer versions, so an env.py that must run against a range of releases keeps it.
2. ``include_name=exclude_from_autogenerate`` — keeps ``alembic revision --autogenerate`` from proposing a
   ``drop_table`` for the plugin's own ``static_data_version`` bookkeeping table.

Note on CSV paths: ``op.load_static_data()`` passes a relative path straight to ``open()``, so it resolves
against the *working directory*. Each revision therefore builds an absolute path from its own ``__file__``
(see ``versions/0002_load_currencies.py``) rather than importing a constant from here — Alembic loads this
file by path, so it is not importable as a module.
"""

from __future__ import annotations

from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

from alembic_static_data import exclude_from_autogenerate
from alembic_static_data import register_operations

register_operations()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The example owns one table; a real project would import its models' MetaData here.
target_metadata = sa.MetaData()
sa.Table(
    "currency",
    target_metadata,
    sa.Column("code", sa.String(3), primary_key=True),
    sa.Column("name", sa.String(50), nullable=False),
    sa.Column("decimals", sa.Integer, nullable=True),
    sa.Column("active", sa.Boolean, nullable=True),
    sa.Column("introduced", sa.Date, nullable=True),
)


def run_migrations_offline() -> None:
    """Offline (``--sql``) mode. ``op.load_static_data()`` refuses to run here: it has to read the database
    to decide what to insert, so there is no SQL script it could emit."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_name=exclude_from_autogenerate,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connectable = sa.create_engine(url, poolclass=sa.pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=exclude_from_autogenerate,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
