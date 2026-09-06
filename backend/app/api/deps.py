"""Request-scoped dependencies: application context, the authenticated
principal, and command preconditions carried in HTTP headers.

Actor identity is never read from request bodies. A `Principal` is derived
server-side from configured authentication; when none is configured the only
way to run commands is an explicitly configured demo principal (dry-run/tests).
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Protocol

from fastapi import Depends, Header, Request
from sqlalchemy.engine import Engine

from app.domain.errors import DomainError, ErrorCode
from app.domain.models import Actor, RepositoryScope
from app.domain.ports import UnitOfWorkFactory
from app.domain.states import ActorRole
from app.domain.transitions import TransitionService

COMMAND_SOURCE = "api"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
IF_MATCH_HEADER = "If-Match"
_IF_MATCH = re.compile(r'^(?:W/)?"?(\d{1,18})"?$')
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9._~+/=-]{8,512}$")


class AuthenticationError(DomainError):
    """Missing or invalid credentials (401). Distinct from an authenticated
    actor lacking permission, which the domain reports as 403."""

    def __init__(self, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(ErrorCode.UNAUTHORIZED_ACTOR, message, details)

    @property
    def http_status(self) -> int:
        return 401


@dataclass(frozen=True)
class Principal:
    login: str
    role: ActorRole
    source: str

    def actor(self) -> Actor:
        return Actor(role=self.role, login=self.login)


class Authenticator(Protocol):
    def authenticate(self, request: Request) -> Principal | None:
        """Return the principal for valid credentials, `None` when the request
        carries no credentials, and raise `AuthenticationError` for bad ones."""


@dataclass(frozen=True)
class StaticTokenAuthenticator:
    """Bearer tokens mapped to principals; suitable for operators/service
    accounts behind TLS. Comparison is constant-time per configured token."""

    tokens: Mapping[str, Principal]

    def authenticate(self, request: Request) -> Principal | None:
        header = request.headers.get("authorization")
        if header is None:
            return None
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer" or not credential:
            raise AuthenticationError("unsupported authorization scheme")
        credential = credential.strip()
        matched: Principal | None = None
        for token, principal in self.tokens.items():
            if hmac.compare_digest(token.encode(), credential.encode()):
                matched = principal
        if matched is None:
            raise AuthenticationError("invalid bearer token")
        return Principal(login=matched.login, role=matched.role, source="bearer")


@dataclass(frozen=True)
class AuthConfig:
    authenticator: Authenticator | None = None
    demo_principal: Principal | None = None

    @property
    def enabled(self) -> bool:
        return self.authenticator is not None or self.demo_principal is not None


def parse_token_table(raw: str) -> StaticTokenAuthenticator:
    """Parse `token:login:role[;token:login:role...]` (e.g. RELAY_AUTH_TOKENS)."""
    tokens: dict[str, Principal] = {}
    for entry in filter(None, (part.strip() for part in raw.split(";"))):
        pieces = entry.split(":")
        if len(pieces) != 3:
            raise ValueError("auth token entries must be token:login:role")
        token, login, role = (p.strip() for p in pieces)
        if not _TOKEN_CHARS.match(token):
            raise ValueError("auth token must be 8-512 URL-safe characters")
        if not login:
            raise ValueError("auth token login must not be empty")
        tokens[token] = Principal(login=login, role=ActorRole(role), source="bearer")
    if not tokens:
        raise ValueError("no auth tokens configured")
    return StaticTokenAuthenticator(tokens)


def parse_demo_principal(raw: str) -> Principal:
    login, sep, role = raw.partition(":")
    if not sep or not login.strip():
        raise ValueError("demo principal must be login:role")
    return Principal(login=login.strip(), role=ActorRole(role.strip()), source="demo")


@dataclass
class AppContext:
    engine: Engine
    uow_factory: UnitOfWorkFactory
    service: TransitionService
    scope: RepositoryScope
    database_url: str
    auth: AuthConfig = field(default_factory=AuthConfig)


def get_context(request: Request) -> AppContext:
    ctx = request.app.state.context
    if not isinstance(ctx, AppContext):
        raise RuntimeError("application context is not configured")
    return ctx


Ctx = Annotated[AppContext, Depends(get_context)]


def get_principal(request: Request, ctx: Ctx) -> Principal:
    auth = ctx.auth
    if auth.authenticator is not None:
        principal = auth.authenticator.authenticate(request)
        if principal is not None:
            return principal
    elif request.headers.get("authorization") is not None:
        raise AuthenticationError("authentication is not configured for this deployment")
    if auth.demo_principal is not None:
        return auth.demo_principal
    raise AuthenticationError(
        "commands require an authenticated principal",
        {"hint": "configure RELAY_AUTH_TOKENS, or RELAY_MODE=demo for dry runs"},
    )


Caller = Annotated[Principal, Depends(get_principal)]

READER_ROLES: frozenset[ActorRole] = frozenset(
    {ActorRole.OPERATOR, ActorRole.SECURITY, ActorRole.SYSTEM}
)


def get_reader(caller: Caller) -> Principal:
    """Issue/session/analytics reads may expose security-private flows, so they
    require an operator-class principal; health/readiness/workflow stay public."""
    if caller.role not in READER_ROLES:
        raise DomainError(
            ErrorCode.UNAUTHORIZED_ACTOR,
            "reading issues, sessions and analytics requires an operator principal",
            {"role": caller.role.value},
        )
    return caller


Reader = Annotated[Principal, Depends(get_reader)]


@dataclass(frozen=True)
class CommandHeaders:
    idempotency_key: str
    expected_version: int | None

    def required_version(self) -> int:
        if self.expected_version is None:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                f"{IF_MATCH_HEADER} header is required when mutating an existing resource",
                {"header": IF_MATCH_HEADER},
            )
        return self.expected_version


def get_command_headers(
    idempotency_key: Annotated[
        str | None, Header(alias=IDEMPOTENCY_KEY_HEADER, max_length=200)
    ] = None,
    if_match: Annotated[str | None, Header(alias=IF_MATCH_HEADER, max_length=40)] = None,
) -> CommandHeaders:
    if idempotency_key is None or not idempotency_key.strip():
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            f"{IDEMPOTENCY_KEY_HEADER} header is required for commands",
            {"header": IDEMPOTENCY_KEY_HEADER},
        )
    expected_version: int | None = None
    if if_match is not None:
        match = _IF_MATCH.match(if_match.strip())
        if match is None:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                f'{IF_MATCH_HEADER} must be a resource version such as "7"',
                {"header": IF_MATCH_HEADER},
            )
        expected_version = int(match.group(1))
    return CommandHeaders(
        idempotency_key=idempotency_key.strip(), expected_version=expected_version
    )


Preconditions = Annotated[CommandHeaders, Depends(get_command_headers)]
