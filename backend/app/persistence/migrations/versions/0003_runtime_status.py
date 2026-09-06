"""runtime component heartbeats

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_status",
        sa.Column("component", sa.String(length=60), nullable=False),
        sa.Column("instance_id", sa.String(length=200), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("component"),
    )


def downgrade() -> None:
    op.drop_table("runtime_status")
