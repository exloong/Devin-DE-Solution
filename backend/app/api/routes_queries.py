from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select, text

from app.api import queries
from app.api.deps import Ctx
from app.api.schemas import (
    AnalyticsSummary,
    HealthResponse,
    IssueDetail,
    IssueListResponse,
    ReadyResponse,
    SessionDetail,
    SessionListResponse,
    WorkflowResponse,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.states import WORKFLOW_VERSION, IssueState

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
def health(ctx: Ctx) -> HealthResponse:
    return HealthResponse(
        status="ok", workflow_version=WORKFLOW_VERSION, repository=ctx.scope.full_name
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(ctx: Ctx) -> ReadyResponse:
    try:
        with ctx.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            version = conn.execute(select(text("version_num")).select_from(text("alembic_version")))
            revision = version.scalar_one_or_none()
    except Exception as exc:  # readiness must never raise
        raise DomainError(
            ErrorCode.INTERNAL, "database is not ready", {"reason": type(exc).__name__}
        ) from exc
    if revision is None:
        raise DomainError(ErrorCode.INTERNAL, "migrations have not been applied")
    return ReadyResponse(
        status="ready", database="ok", migrations=str(revision), dry_run=ctx.scope.dry_run
    )


@router.get("/issues", response_model=IssueListResponse)
def list_issues(
    ctx: Ctx,
    state: IssueState | None = None,
    owner_team: Annotated[str | None, Query(max_length=100)] = None,
) -> IssueListResponse:
    with ctx.uow_factory() as uow:
        items = queries.list_issues(uow, state=state, owner_team=owner_team)
    return IssueListResponse(items=items, total=len(items))


@router.get("/issues/{issue_id}", response_model=IssueDetail)
def get_issue(issue_id: uuid.UUID, ctx: Ctx) -> IssueDetail:
    with ctx.uow_factory() as uow:
        return queries.issue_detail(uow, issue_id)


@router.get("/sessions", response_model=SessionListResponse)
def list_sessions(
    ctx: Ctx,
    issue_id: uuid.UUID | None = None,
    state: Annotated[str | None, Query(max_length=30)] = None,
) -> SessionListResponse:
    with ctx.uow_factory() as uow:
        items = queries.list_sessions(uow, issue_id=issue_id, state=state)
    return SessionListResponse(items=items, total=len(items))


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: uuid.UUID, ctx: Ctx) -> SessionDetail:
    with ctx.uow_factory() as uow:
        return queries.session_detail(uow, session_id)


@router.get("/workflow", response_model=WorkflowResponse)
def workflow() -> WorkflowResponse:
    return queries.workflow()


@router.get("/analytics/summary", response_model=AnalyticsSummary)
def analytics(ctx: Ctx) -> AnalyticsSummary:
    with ctx.uow_factory() as uow:
        return queries.analytics(uow)
