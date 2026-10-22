"""Create the currency table.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "currency",
        sa.Column("code", sa.String(3), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("decimals", sa.Integer, nullable=True),
        sa.Column("active", sa.Boolean, nullable=True),
        sa.Column("introduced", sa.Date, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("currency")
