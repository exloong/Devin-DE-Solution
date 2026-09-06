from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.engine import Engine

from app.domain.models import RepositoryScope
from app.domain.ports import UnitOfWorkFactory
from app.domain.transitions import TransitionService

COMMAND_SOURCE = "api"


@dataclass
class AppContext:
    engine: Engine
    uow_factory: UnitOfWorkFactory
    service: TransitionService
    scope: RepositoryScope
    database_url: str


def get_context(request: Request) -> AppContext:
    ctx = request.app.state.context
    if not isinstance(ctx, AppContext):
        raise RuntimeError("application context is not configured")
    return ctx


Ctx = Annotated[AppContext, Depends(get_context)]
