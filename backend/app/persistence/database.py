from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

from app.persistence.tables import metadata

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        engine = create_engine(
            url,
            connect_args=connect_args,
            poolclass=StaticPool if ":memory:" in url else None,
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
            cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return engine
    return create_engine(url, future=True)


def alembic_config(engine: Engine, *, offline_url: str | None = None) -> Config:
    """Online migrations receive the engine via ``attributes`` so credentials never
    enter the config string space. ``offline_url`` (SQL script generation) must be
    supplied explicitly and must not carry a password."""
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection_engine"] = engine
    if offline_url is not None:
        if make_url(offline_url).password is not None:
            raise ValueError("offline migration URL must not contain a password")
        cfg.set_main_option("sqlalchemy.url", offline_url)
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Apply Alembic migrations. Works for SQLite (tests) and PostgreSQL."""
    command.upgrade(alembic_config(engine), revision)


def create_all(engine: Engine) -> None:
    """Schema shortcut for ephemeral test databases; production uses `upgrade`."""
    metadata.create_all(engine)
