"""Alembic plugin module: importing it registers the ``load_static_data`` operation.

Alembic changed how it loads an ``alembic.plugins`` entry point, so this module is deliberately both a
module with ``setup()`` and a one-element collection of modules; see ``_PluginModule``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from types import ModuleType
from typing import TYPE_CHECKING

import alembic_static_data.operations  # noqa: F401  (import registers the operation)

if TYPE_CHECKING:
    from alembic.runtime.plugins import Plugin


def setup(plugin: Plugin) -> None:
    """Called by Alembic with a ``Plugin`` object when the entry point is loaded.

    The operation is registered as a side effect of importing ``alembic_static_data.operations``; this plugin
    contributes no autogenerate comparators, so there is nothing to add to ``plugin``.
    """


class _PluginModule(ModuleType):
    """Module type that also iterates as a collection containing just the module itself.

    Alembic's entry-point loader expects two different things depending on the version:

    * 1.18.0 (first release with plugins) through 1.20.0 ran ``for mod in entrypoint.load()``, so a plain
      module raises ``TypeError`` while ``import alembic`` is running.
    * 1.20.1 (alembic #1873) expects the documented single module with ``setup()``, and accepts the old
      collection form only with a deprecation warning.

    Being a ``ModuleType`` subclass takes the module branch on 1.20.1+ (``isinstance(loaded, ModuleType)``),
    and being iterable satisfies the older loader, which then calls ``setup()`` on this same module. Alembic
    before 1.18 has no plugin system at all; there, ``alembic_static_data.register_operations()`` does the job.
    """

    def __iter__(self) -> Iterator[ModuleType]:
        return iter((self,))


sys.modules[__name__].__class__ = _PluginModule
