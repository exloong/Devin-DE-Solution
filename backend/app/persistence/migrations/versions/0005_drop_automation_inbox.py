"""drop automation inbox columns: both relay automations fire natively inside Devin

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("devin_automations") as batch:
        batch.drop_column("inbox_secret")
        batch.drop_column("inbox_url")


def downgrade() -> None:
    with op.batch_alter_table("devin_automations") as batch:
        batch.add_column(
            sa.Column("inbox_url", sa.String(length=500), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("inbox_secret", sa.String(length=500), nullable=True))
