from __future__ import annotations

from alembic import context
from app.persistence.tables import metadata
from sqlalchemy.engine import Engine

config = context.config
target_metadata = metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "offline migrations require an explicit, password-free sqlalchemy.url "
            "(see app.persistence.database.alembic_config(offline_url=...))"
        )
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = config.attributes.get("connection_engine")
    if not isinstance(engine, Engine):
        raise RuntimeError(
            "online migrations must receive an Engine via config.attributes['connection_engine']"
        )
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
