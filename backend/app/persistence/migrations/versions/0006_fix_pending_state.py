"""rename issue state fix_authorized -> fix_pending: reproduction alone starts the fix

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def _rename(old: str, new: str) -> None:
    op.execute(
        sa.text("UPDATE issues SET state = :new WHERE state = :old").bindparams(old=old, new=new)
    )


def upgrade() -> None:
    _rename("fix_authorized", "fix_pending")


def downgrade() -> None:
    _rename("fix_pending", "fix_authorized")
