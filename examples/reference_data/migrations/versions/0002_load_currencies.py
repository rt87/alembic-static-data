"""Load the currency reference data from CSV.

Re-running this revision after editing data/currency.csv reloads it; re-running it unchanged does nothing.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# Resolved from this file, not from the working directory: `op.load_static_data()` passes a relative path
# straight through to open(), so "data/currency.csv" would only work when alembic is run from the project
# root. Importing a constant from env.py is not an option - Alembic loads env.py by path, so it is not an
# importable module (`ModuleNotFoundError: No module named 'env'`).
CSV_PATH = Path(__file__).resolve().parents[2] / "data" / "currency.csv"


def upgrade() -> None:
    # Returns UpsertResult(...) and logs it at INFO. Insert/update only - rows absent from the CSV are kept,
    # unless delete_missing=True is passed.
    op.load_static_data("currency", CSV_PATH, natural_keys=["code"])


def downgrade() -> None:
    # 0001's downgrade drops the table, so the recorded checksum has to go too, or a later upgrade would
    # see a matching checksum and skip the reload - leaving an empty table behind.
    op.forget_static_data("currency", CSV_PATH)
