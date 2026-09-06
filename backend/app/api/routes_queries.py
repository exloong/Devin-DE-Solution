from __future__ import annotations

import uuid
from datetime import timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select, text

from app.api import queries
from app.api.deps import Ctx, Reader
from app.api.schemas import (
    AnalyticsSummary,
    HealthResponse,
    IssueDetail,
    IssuePage,
    ReadyResponse,
    SessionDetail,
    SessionPage,
    WorkflowDefinition,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.states import WORKFLOW_VERSION, IssueState, SessionState
from app.persistence import tables

router = APIRouter()
APP_VERSION = "0.1.0"


@router.get("/health", response_model=HealthResponse)
def health(ctx: Ctx) -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=APP_VERSION,
        workflow_version=WORKFLOW_VERSION,
        repository=ctx.scope.full_name,
    )


@router.get("/ready", response_model=ReadyResponse)
def ready(ctx: Ctx) -> ReadyResponse:
    try:
        now = ctx.service.clock.now()
        with ctx.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            version = conn.execute(select(text("version_num")).select_from(text("alembic_version")))
            revision = version.scalar_one_or_none()
            heartbeat = conn.execute(
                select(tables.runtime_status.c.updated_at).where(
                    tables.runtime_status.c.component == "worker"
                )
            ).scalar_one_or_none()
    except Exception as exc:  # readiness must never leak driver details
        raise DomainError(
            ErrorCode.INTERNAL, "database is not ready", {"reason": type(exc).__name__}
        ) from exc
    if not isinstance(revision, str):
        raise DomainError(ErrorCode.INTERNAL, "migrations have not been applied")
    worker = "unavailable"
    if heartbeat is not None:
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        worker = "ok" if now - heartbeat <= timedelta(seconds=30) else "stale"
    return ReadyResponse(
        database="ok",
        worker=worker,
        last_worker_heartbeat_at=heartbeat,
        migrations=revision,
        dry_run=ctx.scope.dry_run,
    )


@router.get("/issues", response_model=IssuePage)
def list_issues(
    ctx: Ctx,
    _reader: Reader,
    state: Annotated[list[IssueState] | None, Query()] = None,
    owner: Annotated[str | None, Query(max_length=100)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> IssuePage:
    with ctx.uow_factory() as uow:
        items = queries.list_issues(
            uow, states=state or [], owner=owner, search=search, dry_run=ctx.scope.dry_run
        )
    return IssuePage(items=items, total=len(items), generated_at=ctx.service.clock.now())


@router.get("/issues/{issue_id}", response_model=IssueDetail)
def get_issue(issue_id: uuid.UUID, ctx: Ctx, _reader: Reader) -> IssueDetail:
    with ctx.uow_factory() as uow:
        return queries.issue_detail(uow, issue_id, dry_run=ctx.scope.dry_run)


@router.get("/sessions", response_model=SessionPage)
def list_sessions(
    ctx: Ctx,
    _reader: Reader,
    issue_id: uuid.UUID | None = None,
    status: Annotated[list[SessionState] | None, Query()] = None,
) -> SessionPage:
    with ctx.uow_factory() as uow:
        items = queries.list_sessions(
            uow, issue_id=issue_id, statuses=status or [], dry_run=ctx.scope.dry_run
        )
    return SessionPage(items=items, total=len(items), generated_at=ctx.service.clock.now())


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(session_id: uuid.UUID, ctx: Ctx, _reader: Reader) -> SessionDetail:
    with ctx.uow_factory() as uow:
        return queries.session_detail(uow, session_id, dry_run=ctx.scope.dry_run)


@router.get("/workflow", response_model=WorkflowDefinition)
def workflow(ctx: Ctx) -> WorkflowDefinition:
    return queries.workflow(ctx.service.clock.now())


@router.get("/analytics/summary", response_model=AnalyticsSummary)
def analytics(ctx: Ctx, _reader: Reader) -> AnalyticsSummary:
    with ctx.uow_factory() as uow:
        return queries.analytics(uow, ctx.service.clock.now())
