"""Command endpoints. Every command becomes a typed Event applied through the
transition service; the request's idempotency_key is the delivery id."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api import queries
from app.api.deps import COMMAND_SOURCE, AppContext, Ctx
from app.api.schemas import (
    CommandRequest,
    CommandResponse,
    DryRunRequest,
    DryRunResponse,
    OwnerDecisionRequest,
    ReporterResponseRequest,
    RetryRequest,
    SessionCancelRequest,
    SessionMessageRequest,
)
from app.domain import scenarios
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import Actor, Event
from app.domain.states import ActorRole, EventType
from app.domain.transitions import TransitionResult

router = APIRouter()

DRY_RUN_NUMBER_BASE = 900_000


def _apply(
    ctx: AppContext,
    *,
    issue_id: uuid.UUID | None,
    event_type: EventType,
    actor: Actor,
    request: CommandRequest,
    payload: dict[str, object],
) -> CommandResponse:
    event = Event(
        issue_id=issue_id,
        source=COMMAND_SOURCE,
        delivery_id=request.idempotency_key,
        type=event_type,
        actor=actor,
        payload=payload,
    )
    with ctx.uow_factory() as uow:
        result: TransitionResult = ctx.service.apply(
            uow, event, expected_version=request.expected_version
        )
        summary = queries.issue_summary(uow, result.issue) if result.issue else None
    return CommandResponse(
        event_id=result.event.id,
        duplicate=result.duplicate,
        status=result.attempt.status.value,
        transition=result.attempt.transition,
        from_state=result.attempt.from_state,
        to_state=result.attempt.to_state,
        detail=result.attempt.detail,
        issue=summary,
    )


def _session_issue(ctx: AppContext, session_id: uuid.UUID) -> uuid.UUID:
    with ctx.uow_factory() as uow:
        session = uow.get_session(session_id)
    if session is None:
        raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": str(session_id)})
    return session.issue_id


@router.post("/dry-runs", response_model=DryRunResponse, status_code=201)
def create_dry_run(body: DryRunRequest, ctx: Ctx) -> DryRunResponse:
    if body.scenario is not None:
        seeded = scenarios.seed(ctx.uow_factory, ctx.service, only=body.scenario)
        issue_id = seeded.issues.get(body.scenario)
        with ctx.uow_factory() as uow:
            issue = uow.get_issue(issue_id) if issue_id else None
            summary = queries.issue_summary(uow, issue) if issue else None
        return DryRunResponse(
            scenario=body.scenario,
            issue=summary,
            applied=seeded.applied,
            duplicates=seeded.duplicates,
            rejected=seeded.rejected,
        )

    number = body.number
    if number is None:
        with ctx.uow_factory() as uow:
            existing = [i.external_number for i in uow.list_issues()]
        number = max([DRY_RUN_NUMBER_BASE, *existing]) + 1
    payload: dict[str, object] = {
        "repository": ctx.scope.full_name,
        "number": number,
        "title": body.title,
        "body": body.body,
        "labels": list(body.labels),
        "reporter": body.reporter,
        "category": body.category,
        "owner_team": body.owner_team,
        "dry_run": True,
    }
    response = _apply(
        ctx,
        issue_id=None,
        event_type=EventType.ISSUE_OPENED,
        actor=Actor(role=ActorRole.SYSTEM, login=body.actor_login),
        request=body,
        payload=payload,
    )
    return DryRunResponse(
        scenario=None,
        issue=response.issue,
        applied=0 if response.duplicate else 1,
        duplicates=1 if response.duplicate else 0,
        rejected=0,
    )


@router.post("/issues/{issue_id}/reporter-responses", response_model=CommandResponse)
def reporter_response(
    issue_id: uuid.UUID, body: ReporterResponseRequest, ctx: Ctx
) -> CommandResponse:
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.REPORTER_RESPONSE,
        actor=Actor(role=ActorRole.REPORTER, login=body.actor_login),
        request=body,
        payload={"answers": [a.model_dump() for a in body.answers]},
    )


@router.post("/issues/{issue_id}/decisions", response_model=CommandResponse)
def owner_decision(issue_id: uuid.UUID, body: OwnerDecisionRequest, ctx: Ctx) -> CommandResponse:
    payload: dict[str, object] = {"decision": body.decision.value, "rationale": body.rationale}
    if body.scope is not None:
        payload["scope"] = body.scope
    if body.reclassify_to is not None:
        payload["reclassify_to"] = body.reclassify_to.value
    if body.field is not None:
        payload["field"] = body.field
    if body.prompt is not None:
        payload["prompt"] = body.prompt
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.OWNER_DECISION,
        actor=Actor(role=body.actor_role, login=body.actor_login),
        request=body,
        payload=payload,
    )


@router.post("/issues/{issue_id}/retry", response_model=CommandResponse)
def retry(issue_id: uuid.UUID, body: RetryRequest, ctx: Ctx) -> CommandResponse:
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.RETRY_REQUESTED,
        actor=Actor(role=ActorRole.OPERATOR, login=body.actor_login),
        request=body,
        payload={"reason": body.reason},
    )


@router.post("/sessions/{session_id}/cancel-requests", response_model=CommandResponse)
def cancel_session(session_id: uuid.UUID, body: SessionCancelRequest, ctx: Ctx) -> CommandResponse:
    return _apply(
        ctx,
        issue_id=_session_issue(ctx, session_id),
        event_type=EventType.SESSION_CANCEL_REQUESTED,
        actor=Actor(role=ActorRole.OPERATOR, login=body.actor_login),
        request=body,
        payload={"session_id": str(session_id), "reason": body.reason},
    )


@router.post("/sessions/{session_id}/messages", response_model=CommandResponse)
def message_session(
    session_id: uuid.UUID, body: SessionMessageRequest, ctx: Ctx
) -> CommandResponse:
    return _apply(
        ctx,
        issue_id=_session_issue(ctx, session_id),
        event_type=EventType.SESSION_MESSAGE,
        actor=Actor(role=ActorRole.OPERATOR, login=body.actor_login),
        request=body,
        payload={"session_id": str(session_id), "body": body.body},
    )
