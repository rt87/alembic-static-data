"""Alembic plugin module: importing it registers the ``load_static_data`` operation."""

from __future__ import annotations

from typing import TYPE_CHECKING

import alembic_static_data.operations  # noqa: F401  (import registers the operation)

if TYPE_CHECKING:
    from alembic.runtime.plugins import Plugin


def setup(plugin: Plugin) -> None:
    """Called by Alembic with a ``Plugin`` object when the entry point is loaded.

    The operation is registered as a side effect of importing ``alembic_static_data.operations``; this plugin
    contributes no autogenerate comparators, so there is nothing to add to ``plugin``.
    """
