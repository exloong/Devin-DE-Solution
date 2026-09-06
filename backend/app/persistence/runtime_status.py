"""Liveness markers shared by the API and worker (``runtime_status`` table).

Each component owns one row keyed by name. ``instance_id`` carries the writer's
identity (worker hostname, provider mode) and ``updated_at`` the last touch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Connection, Engine

from app.persistence import tables

WORKER = "worker"
GITHUB_WEBHOOK = "github_webhook"
PROVIDER_GITHUB = "provider:github"
PROVIDER_DEVIN = "provider:devin"


@dataclass(frozen=True)
class StatusRow:
    component: str
    instance_id: str
    updated_at: datetime


def touch(conn: Connection, component: str, instance_id: str, now: datetime) -> None:
    existing = conn.execute(
        select(tables.runtime_status.c.component).where(
            tables.runtime_status.c.component == component
        )
    ).first()
    values = {"instance_id": instance_id, "updated_at": now}
    if existing is None:
        conn.execute(tables.runtime_status.insert().values(component=component, **values))
    else:
        conn.execute(
            tables.runtime_status.update()
            .where(tables.runtime_status.c.component == component)
            .values(**values)
        )


def touch_all(engine: Engine, components: dict[str, str], now: datetime) -> None:
    with engine.begin() as conn:
        for component, instance_id in components.items():
            touch(conn, component, instance_id, now)


def read_all(conn: Connection) -> dict[str, StatusRow]:
    rows = conn.execute(
        select(
            tables.runtime_status.c.component,
            tables.runtime_status.c.instance_id,
            tables.runtime_status.c.updated_at,
        )
    ).all()
    out: dict[str, StatusRow] = {}
    for component, instance_id, updated_at in rows:
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        out[str(component)] = StatusRow(str(component), str(instance_id), updated_at)
    return out
