"""Command endpoints. Every command becomes a typed Event applied through the
transition service. Actor identity comes from the authenticated principal,
the `Idempotency-Key` header is the delivery id, and `If-Match` carries the
expected resource version. Request bodies never carry identity."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.deps import (
    COMMAND_SOURCE,
    AppContext,
    Caller,
    CommandHeaders,
    Ctx,
    Preconditions,
    Principal,
)
from app.api.schemas import (
    CommandAccepted,
    DryRunRequest,
    OwnerDecisionRequest,
    ReporterResponseRequest,
    RetryRequest,
    SessionCancelRequest,
    SessionMessageRequest,
)
from app.domain import scenarios
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import Actor, AgentSession, Event
from app.domain.states import ActorRole, EventType, IssueState
from app.domain.transitions import TransitionResult

router = APIRouter()

DRY_RUN_NUMBER_BASE = 900_000

_RECLASSIFY: dict[str, IssueState] = {
    "duplicate": IssueState.DUPLICATE,
    "not_a_bug": IssueState.NOT_A_BUG,
    "unsupported": IssueState.UNSUPPORTED,
    "support": IssueState.UNSUPPORTED,
}


def _apply(
    ctx: AppContext,
    *,
    issue_id: uuid.UUID | None,
    event_type: EventType,
    caller: Principal,
    headers: CommandHeaders,
    payload: dict[str, object],
    issue_revision: int | None = None,
    actor: Actor | None = None,
) -> CommandAccepted:
    expected_version = headers.required_version() if issue_id is not None else None
    event = Event(
        issue_id=issue_id,
        source=COMMAND_SOURCE,
        delivery_id=headers.idempotency_key,
        type=event_type,
        actor=caller.actor() if actor is None else actor,
        issue_revision=issue_revision,
        payload=payload,
    )
    with ctx.uow_factory() as uow:
        result: TransitionResult = ctx.service.apply(uow, event, expected_version=expected_version)
    return _accepted(ctx, result)


def _apply_session(
    ctx: AppContext,
    *,
    session_id: uuid.UUID,
    event_type: EventType,
    caller: Principal,
    headers: CommandHeaders,
    payload: dict[str, object],
) -> CommandAccepted:
    """Session mutations bind `If-Match` to session.version and return the session."""
    expected = headers.required_version()
    event = Event(
        issue_id=_session_issue(ctx, session_id),
        source=COMMAND_SOURCE,
        delivery_id=headers.idempotency_key,
        type=event_type,
        actor=caller.actor(),
        payload={"session_id": str(session_id), **payload},
    )
    with ctx.uow_factory() as uow:
        result = ctx.service.apply(uow, event, expected_session_version=expected)
        session = uow.get_session(session_id)
    if session is None:
        raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": str(session_id)})
    return _accepted(ctx, result, resource=session)


def _accepted(
    ctx: AppContext,
    result: TransitionResult,
    *,
    resource: AgentSession | None = None,
    **extra: object,
) -> CommandAccepted:
    issue = result.issue
    if resource is not None:
        resource_id, resource_version = resource.id, resource.version
    elif issue is not None:
        resource_id, resource_version = issue.id, issue.version
    else:
        resource_id, resource_version = result.event.id, 0
    return CommandAccepted(
        event_id=result.event.id,
        resource_id=resource_id,
        resource_version=resource_version,
        accepted_at=ctx.service.clock.now(),
        processing="complete",
        duplicate=result.duplicate,
        status=result.attempt.status.value,
        transition=result.attempt.transition.value if result.attempt.transition else None,
        from_state=result.attempt.from_state,
        to_state=result.attempt.to_state,
        detail=result.attempt.detail,
        **extra,
    )


def _session_issue(ctx: AppContext, session_id: uuid.UUID) -> uuid.UUID:
    with ctx.uow_factory() as uow:
        session = uow.get_session(session_id)
    if session is None:
        raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": str(session_id)})
    return session.issue_id


@router.post("/dry-runs", response_model=CommandAccepted, status_code=201)
def create_dry_run(
    body: DryRunRequest, ctx: Ctx, caller: Caller, headers: Preconditions
) -> CommandAccepted:
    if caller.role not in (ActorRole.OPERATOR, ActorRole.SYSTEM):
        raise DomainError(
            ErrorCode.UNAUTHORIZED_ACTOR,
            "dry runs require an operator principal",
            {"role": caller.role.value},
        )
    if body.scenario is not None:
        seeded = scenarios.seed(ctx.uow_factory, ctx.service, only=body.scenario)
        issue_id = seeded.issues.get(body.scenario)
        if issue_id is None:
            raise DomainError(ErrorCode.INTERNAL, "scenario did not produce an issue")
        with ctx.uow_factory() as uow:
            issue = uow.get_issue(issue_id)
            if issue is None:
                raise DomainError(ErrorCode.INTERNAL, "seeded issue not found")
            version = issue.version
        return CommandAccepted(
            event_id=uuid.uuid5(issue_id, headers.idempotency_key),
            resource_id=issue_id,
            resource_version=version,
            accepted_at=ctx.service.clock.now(),
            processing="complete",
            duplicate=seeded.applied == 0,
            status="applied" if seeded.applied else "noop",
            to_state=issue.state,
            detail=f"scenario {body.scenario} seeded",
            scenario=body.scenario,
            applied=seeded.applied,
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
        "requested_by": caller.login,
    }
    # Intake is a system transition; the authenticated operator is recorded as
    # the requester, never as the intake actor.
    return _apply(
        ctx,
        issue_id=None,
        event_type=EventType.ISSUE_OPENED,
        caller=caller,
        headers=headers,
        payload=payload,
        actor=Actor(role=ActorRole.SYSTEM, login=f"dry-run:{caller.login}"),
    )


@router.post("/issues/{issue_id}/responses", response_model=CommandAccepted)
def reporter_response(
    issue_id: uuid.UUID,
    body: ReporterResponseRequest,
    ctx: Ctx,
    caller: Caller,
    headers: Preconditions,
) -> CommandAccepted:
    answers: list[dict[str, object]]
    if body.answers is not None:
        answers = [a.model_dump() for a in body.answers]
    else:
        if body.question_id is None or body.response is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "question_id and response are required")
        with ctx.uow_factory() as uow:
            question = uow.get_question(body.question_id)
        if question is None or question.issue_id != issue_id:
            raise DomainError(
                ErrorCode.NOT_FOUND, "question not found", {"question_id": str(body.question_id)}
            )
        if body.response.kind == "answer":
            answers = [{"field": question.field, "answer": body.response.value}]
        else:
            answers = [
                {"field": question.field, "unavailable": True, "reason": body.response.reason}
            ]
    payload: dict[str, object] = {"answers": answers}
    if body.override_rationale is not None:
        payload["override_rationale"] = body.override_rationale
    demo_actor: Actor | None = None
    if caller.source == "demo":
        with ctx.uow_factory() as uow:
            issue = uow.get_issue(issue_id)
        if issue is None:
            raise DomainError(ErrorCode.NOT_FOUND, "issue not found")
        demo_actor = Actor(role=ActorRole.REPORTER, login=issue.reporter_login)
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.REPORTER_RESPONSE,
        caller=caller,
        headers=headers,
        payload=payload,
        issue_revision=body.issue_revision,
        actor=demo_actor,
    )


@router.post("/issues/{issue_id}/decisions", response_model=CommandAccepted)
def owner_decision(
    issue_id: uuid.UUID,
    body: OwnerDecisionRequest,
    ctx: Ctx,
    caller: Caller,
    headers: Preconditions,
) -> CommandAccepted:
    payload: dict[str, object] = {"decision": body.kind.value, "rationale": body.rationale}
    if body.scope is not None:
        payload["scope"] = body.scope
    if body.reclassify_as is not None:
        payload["reclassify_to"] = _RECLASSIFY[body.reclassify_as].value
    if body.field is not None:
        payload["field"] = body.field
    if body.prompt is not None:
        payload["prompt"] = body.prompt
    if body.pr_number is not None:
        payload["pr_number"] = body.pr_number
    if body.head_sha is not None:
        payload["head_sha"] = body.head_sha
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.OWNER_DECISION,
        caller=caller,
        headers=headers,
        payload=payload,
        issue_revision=body.issue_revision,
        actor=(
            Actor(role=ActorRole.OWNER, login="owner-demo")
            if caller.source == "demo"
            else None
        ),
    )


@router.post("/issues/{issue_id}/actions/retry", response_model=CommandAccepted)
def retry(
    issue_id: uuid.UUID, body: RetryRequest, ctx: Ctx, caller: Caller, headers: Preconditions
) -> CommandAccepted:
    return _apply(
        ctx,
        issue_id=issue_id,
        event_type=EventType.RETRY_REQUESTED,
        caller=caller,
        headers=headers,
        payload={"reason": body.reason},
    )


@router.post("/sessions/{session_id}/actions/cancel", response_model=CommandAccepted)
def cancel_session(
    session_id: uuid.UUID,
    body: SessionCancelRequest,
    ctx: Ctx,
    caller: Caller,
    headers: Preconditions,
) -> CommandAccepted:
    return _apply_session(
        ctx,
        session_id=session_id,
        event_type=EventType.SESSION_CANCEL_REQUESTED,
        caller=caller,
        headers=headers,
        payload={"reason": body.reason},
    )


@router.post("/sessions/{session_id}/messages", response_model=CommandAccepted)
def message_session(
    session_id: uuid.UUID,
    body: SessionMessageRequest,
    ctx: Ctx,
    caller: Caller,
    headers: Preconditions,
) -> CommandAccepted:
    return _apply_session(
        ctx,
        session_id=session_id,
        event_type=EventType.SESSION_MESSAGE,
        caller=caller,
        headers=headers,
        payload={"body": body.body},
    )
