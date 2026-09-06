"""Server-side store for Relay's Devin automation handles (``devin_automations``).

The inbox secret is returned by Devin exactly once, at creation; this table is
the only place Relay keeps it. It is never exposed through the API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.domain.ports import Clock
from app.integrations.devin_automations import AutomationHandle
from app.integrations.tasks import TaskKind
from app.persistence import tables


class DatabaseAutomationSecretStore:
    def __init__(self, engine: Engine, *, clock: Clock) -> None:
        self._engine = engine
        self._clock = clock

    def load(self, kind: TaskKind) -> AutomationHandle | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    select(tables.devin_automations).where(
                        tables.devin_automations.c.kind == kind.value
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return AutomationHandle(
            automation_id=row["automation_id"],
            kind=kind,
            inbox_url=row["inbox_url"],
            inbox_secret=row["inbox_secret"],
            enabled=bool(row["enabled"]),
        )

    def save(self, handle: AutomationHandle) -> None:
        values = {
            "automation_id": handle.automation_id,
            "inbox_url": handle.inbox_url,
            "inbox_secret": handle.inbox_secret,
            "enabled": handle.enabled,
            "updated_at": self._clock.now(),
        }
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(tables.devin_automations.c.kind).where(
                    tables.devin_automations.c.kind == handle.kind.value
                )
            ).first()
            if existing is None:
                conn.execute(
                    tables.devin_automations.insert().values(kind=handle.kind.value, **values)
                )
            else:
                conn.execute(
                    tables.devin_automations.update()
                    .where(tables.devin_automations.c.kind == handle.kind.value)
                    .values(**values)
                )


@dataclass(frozen=True)
class AutomationRecord:
    """A registered automation as exposed by the API: ids only, never the secret."""

    kind: str
    automation_id: str
    enabled: bool
    updated_at: datetime


def read_public(conn: Connection) -> list[AutomationRecord]:
    rows = conn.execute(
        select(
            tables.devin_automations.c.kind,
            tables.devin_automations.c.automation_id,
            tables.devin_automations.c.enabled,
            tables.devin_automations.c.updated_at,
        ).order_by(tables.devin_automations.c.kind)
    ).mappings()
    return [
        AutomationRecord(
            kind=row["kind"],
            automation_id=row["automation_id"],
            enabled=bool(row["enabled"]),
            updated_at=row["updated_at"],
        )
        for row in rows
    ]
