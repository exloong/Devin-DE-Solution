from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import AppContext
from app.api.routes_commands import router as commands_router
from app.api.routes_queries import router as queries_router
from app.domain import scenarios
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import RepositoryScope
from app.domain.ports import Clock
from app.domain.transitions import TransitionService
from app.persistence.database import make_engine, upgrade
from app.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWorkFactory

API_PREFIX = "/api/v1"
DEFAULT_DATABASE_URL = "sqlite:///./relay.sqlite3"


def _error_response(err: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=err.http_status,
        content={"error": {"code": err.code.value, "message": err.message, "details": err.details}},
    )


def create_app(
    *,
    database_url: str | None = None,
    seed_scenarios: bool = False,
    clock: Clock | None = None,
    scope: RepositoryScope | None = None,
) -> FastAPI:
    url = database_url or os.environ.get("RELAY_DATABASE_URL", DEFAULT_DATABASE_URL)
    engine = make_engine(url)
    upgrade(engine)
    repo_scope = scope or RepositoryScope()
    service = TransitionService(scope=repo_scope, clock=clock)
    factory = SqlAlchemyUnitOfWorkFactory(engine)
    context = AppContext(
        engine=engine,
        uow_factory=factory,
        service=service,
        scope=repo_scope,
        database_url=url,
    )
    if seed_scenarios:
        scenarios.seed(factory, service)

    app = FastAPI(title="Relay", version="0.1.0", docs_url=f"{API_PREFIX}/docs")
    app.state.context = context
    app.include_router(queries_router, prefix=API_PREFIX)
    app.include_router(commands_router, prefix=API_PREFIX)

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, err: DomainError) -> JSONResponse:
        return _error_response(err)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, err: RequestValidationError) -> JSONResponse:
        details: dict[str, object] = {
            "errors": [{"loc": list(map(str, e["loc"])), "msg": e["msg"]} for e in err.errors()]
        }
        return _error_response(DomainError(ErrorCode.INVALID_INPUT, "invalid request", details))

    return app


def default_app() -> FastAPI:
    return create_app(seed_scenarios=os.environ.get("RELAY_SEED", "1") == "1")
