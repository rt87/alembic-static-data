"""Idempotent static reference data loading for Alembic migrations."""

from __future__ import annotations

from alembic_static_data.tracking import exclude_from_autogenerate

__all__ = ["exclude_from_autogenerate", "register_operations"]


def register_operations() -> None:
    """Register ``op.load_static_data``, without relying on Alembic's plugin system.

    Needed on Alembic before 1.18, which has no plugin system and so never discovers the ``alembic.plugins``
    entry point; call it from ``env.py``. Harmless and idempotent on newer versions, where importing Alembic
    already registers the operation.
    """
    import alembic_static_data.operations  # noqa: F401  (import registers the operation)
