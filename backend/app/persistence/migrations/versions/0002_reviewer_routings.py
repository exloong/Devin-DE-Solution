"""reviewer routings

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-06 01:40:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reviewer_routings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("issue_id", sa.String(length=36), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("reviewer_routings", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_reviewer_routings_issue_id"), ["issue_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("reviewer_routings", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reviewer_routings_issue_id"))
    op.drop_table("reviewer_routings")
