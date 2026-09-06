from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import (
    AppContext,
    AuthConfig,
    parse_demo_principal,
    parse_token_table,
)
from app.api.routes_commands import router as commands_router
from app.api.routes_queries import router as queries_router
from app.api.routes_webhooks import router as webhooks_router
from app.domain import scenarios
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import RepositoryScope
from app.domain.ports import Clock
from app.domain.transitions import TransitionService
from app.integrations.errors import ContractValidationError, ValidationCode
from app.persistence.database import make_engine, upgrade
from app.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWorkFactory

API_PREFIX = "/api/v1"
DEFAULT_DATABASE_URL = "sqlite:///./relay.sqlite3"
DEFAULT_DEMO_PRINCIPAL = "demo-operator:operator"


def auth_from_env(environ: Mapping[str, str] | None = None) -> AuthConfig:
    """RELAY_AUTH_TOKENS configures bearer principals. RELAY_MODE=demo enables
    the explicit demo principal (RELAY_DEMO_PRINCIPAL, `login:role`) for dry
    runs. With neither set, every command is denied."""
    env = os.environ if environ is None else environ
    tokens = env.get("RELAY_AUTH_TOKENS", "").strip()
    demo = env.get("RELAY_MODE", "").strip().lower() == "demo"
    return AuthConfig(
        authenticator=parse_token_table(tokens) if tokens else None,
        demo_principal=(
            parse_demo_principal(env.get("RELAY_DEMO_PRINCIPAL", DEFAULT_DEMO_PRINCIPAL))
            if demo
            else None
        ),
    )


def _error_response(err: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=err.http_status,
        content={"error": {"code": err.code.value, "message": err.message, "details": err.details}},
    )


def _contract_error_response(err: ContractValidationError) -> JSONResponse:
    statuses = {
        ValidationCode.INVALID_SIGNATURE: 401,
        ValidationCode.UNAUTHORIZED_REPOSITORY: 403,
        ValidationCode.DUPLICATE_DELIVERY: 409,
    }
    return JSONResponse(
        status_code=statuses.get(err.code, 400),
        content={"error": {"code": err.code.value, "message": str(err)}},
    )


def create_app(
    *,
    database_url: str | None = None,
    seed_scenarios: bool = False,
    clock: Clock | None = None,
    scope: RepositoryScope | None = None,
    auth: AuthConfig | None = None,
) -> FastAPI:
    url = (
        database_url
        or os.environ.get("DATABASE_URL")
        or os.environ.get("RELAY_DATABASE_URL")
        or DEFAULT_DATABASE_URL
    )
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
        auth=auth if auth is not None else AuthConfig(),
    )
    if seed_scenarios:
        scenarios.seed(factory, service)

    app = FastAPI(title="Relay", version="0.1.0", docs_url=f"{API_PREFIX}/docs")
    app.state.context = context
    app.include_router(queries_router, prefix=API_PREFIX)
    app.include_router(commands_router, prefix=API_PREFIX)
    app.include_router(webhooks_router, prefix=API_PREFIX)

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, err: DomainError) -> JSONResponse:
        return _error_response(err)

    @app.exception_handler(ContractValidationError)
    async def _contract_error(_: Request, err: ContractValidationError) -> JSONResponse:
        return _contract_error_response(err)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, err: RequestValidationError) -> JSONResponse:
        details: dict[str, object] = {
            "errors": [{"loc": list(map(str, e["loc"])), "msg": e["msg"]} for e in err.errors()]
        }
        return _error_response(DomainError(ErrorCode.INVALID_INPUT, "invalid request", details))

    return app


def default_app() -> FastAPI:
    return create_app(seed_scenarios=os.environ.get("RELAY_SEED", "1") == "1", auth=auth_from_env())
