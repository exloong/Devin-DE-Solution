from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import (
    AppContext,
    AuthConfig,
    parse_demo_principal,
    parse_token_table,
)
from app.api.routes_automations import router as automations_router
from app.api.routes_commands import router as commands_router
from app.api.routes_queries import router as queries_router
from app.api.routes_webhooks import router as webhooks_router
from app.domain import scenarios
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import RepositoryScope
from app.domain.ports import Clock
from app.domain.transitions import SystemClock, TransitionService
from app.integrations.devin_automations import (
    AUTOMATION_KINDS,
    DevinAutomationClient,
    FakeDevinAutomationClient,
    LiveDevinAutomationClient,
)
from app.integrations.devin_sessions import FakeDevinSessionAdapter, LiveDevinSessionClient
from app.integrations.errors import ContractValidationError, ValidationCode
from app.integrations.github_client import StaticTokenProvider
from app.integrations.tasks import TaskPolicy
from app.integrations.transport import HttpxTransport
from app.persistence.database import make_engine, upgrade
from app.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWorkFactory

API_PREFIX = "/api/v1"
DEFAULT_DATABASE_URL = "sqlite:///./relay.sqlite3"
DEFAULT_DEMO_PRINCIPAL = "demo-operator:operator"


def _secret_from_env(env: Mapping[str, str], name: str) -> str:
    inline = env.get(name, "").strip()
    file_name = env.get(f"{name}_FILE", "").strip()
    if inline and file_name:
        raise ValueError(f"configure only one of {name} or {name}_FILE")
    if file_name:
        return Path(file_name).read_text(encoding="utf-8").strip()
    return inline


def auth_from_env(environ: Mapping[str, str] | None = None) -> AuthConfig:
    """RELAY_AUTH_TOKENS configures bearer principals. RELAY_MODE=demo enables
    the explicit demo principal (RELAY_DEMO_PRINCIPAL, `login:role`) for dry
    runs. With neither set, every command is denied."""
    env = os.environ if environ is None else environ
    tokens = _secret_from_env(env, "RELAY_AUTH_TOKENS")
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
        ValidationCode.TRANSPORT_FAILURE: 502,
        ValidationCode.MALFORMED_RESPONSE: 502,
    }
    status = statuses.get(err.code, 400)
    # Devin answering 401/403/404 is the operator's problem (token permissions
    # such as ViewOrgAutomations/ManageOrgAutomations, or a deleted automation),
    # not an outage, so those pass through instead of becoming a 502.
    if err.upstream_status in _PASSTHROUGH_UPSTREAM:
        status = _PASSTHROUGH_UPSTREAM[err.upstream_status]
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": err.code.value,
                "message": str(err),
                "upstream_status": err.upstream_status,
            }
        },
    )


_PASSTHROUGH_UPSTREAM = {401: 403, 403: 403, 404: 404}


def demo_automations(clock: Clock) -> FakeDevinAutomationClient:
    """In-memory automations for dry runs, pre-seeded with Relay's two."""
    client = FakeDevinAutomationClient(
        FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400)),
        clock=clock.now,
    )
    for kind in AUTOMATION_KINDS:
        client.ensure_automation(kind, now=clock.now())
    return client


def live_automations_from_env(
    environ: Mapping[str, str] | None = None,
) -> LiveDevinAutomationClient | None:
    """The API's read/write client for Devin Automations; ``None`` when the
    Devin credentials are not configured (the UI then reports it)."""
    env = os.environ if environ is None else environ
    token = _secret_from_env(env, "DEVIN_API_TOKEN")
    org_id = env.get("DEVIN_ORG_ID", "").strip()
    if not token or not org_id:
        return None
    transport = HttpxTransport(
        timeout_seconds=float(env.get("RELAY_HTTP_TIMEOUT_SECONDS", "30")),
        retries=int(env.get("RELAY_HTTP_RETRIES", "2")),
    )
    provider = StaticTokenProvider(token)
    sessions = LiveDevinSessionClient(
        transport=transport,
        token_provider=provider,
        org_id=org_id,
        policy=TaskPolicy(max_wall_seconds=5_400),
    )
    return LiveDevinAutomationClient(
        transport=transport, token_provider=provider, org_id=org_id, sessions=sessions
    )


def create_app(
    *,
    database_url: str | None = None,
    seed_scenarios: bool = False,
    clock: Clock | None = None,
    scope: RepositoryScope | None = None,
    auth: AuthConfig | None = None,
    automations: DevinAutomationClient | None = None,
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
        automations=automations,
    )
    if seed_scenarios:
        scenarios.seed(factory, service)

    app = FastAPI(title="Relay", version="0.1.0", docs_url=f"{API_PREFIX}/docs")
    app.state.context = context
    app.include_router(queries_router, prefix=API_PREFIX)
    app.include_router(commands_router, prefix=API_PREFIX)
    app.include_router(webhooks_router, prefix=API_PREFIX)
    app.include_router(automations_router, prefix=API_PREFIX)

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
    mode = os.environ.get("RELAY_MODE", "live").strip().lower()
    demo = mode == "demo"
    return create_app(
        seed_scenarios=os.environ.get("RELAY_SEED", "0") == "1",
        scope=RepositoryScope(
            default_branch=os.environ.get("GITHUB_DEFAULT_BRANCH", "master"),
            dry_run=demo,
        ),
        auth=auth_from_env(),
        automations=demo_automations(SystemClock()) if demo else live_automations_from_env(),
    )
