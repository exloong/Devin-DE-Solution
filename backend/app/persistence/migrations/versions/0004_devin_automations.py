"""relay-managed devin automations (ids, inbox urls, inbox secrets)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "devin_automations",
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("automation_id", sa.String(length=120), nullable=False),
        sa.Column("inbox_url", sa.String(length=500), nullable=False),
        sa.Column("inbox_secret", sa.String(length=500), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("kind"),
    )


def downgrade() -> None:
    op.drop_table("devin_automations")
