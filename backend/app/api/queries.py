"""Read-side assembly of API responses from the unit of work."""

from __future__ import annotations

import uuid
from collections import Counter

from app.api.schemas import (
    AnalyticsSummary,
    IssueDetail,
    IssueSummary,
    SessionDetail,
    SessionSummary,
    WorkflowResponse,
    WorkflowTransition,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import AttemptStatus, Issue, JobStatus, QuestionStatus
from app.domain.ports import UnitOfWork
from app.domain.states import (
    ACTIVE_AGENT_STATES,
    TARGET_REPOSITORY,
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    WORKFLOW_VERSION,
    IssueState,
)


def repository_name(uow: UnitOfWork) -> str:
    repo = uow.get_repository()
    return repo.full_name if repo else TARGET_REPOSITORY


def issue_summary(uow: UnitOfWork, issue: Issue) -> IssueSummary:
    return IssueSummary.from_issue(
        issue,
        repository=repository_name(uow),
        sessions=list(uow.list_sessions(issue_id=issue.id)),
        questions=list(uow.list_questions(issue.id, issue.revision)),
    )


def list_issues(
    uow: UnitOfWork, *, state: IssueState | None, owner_team: str | None
) -> list[IssueSummary]:
    return [issue_summary(uow, i) for i in uow.list_issues(state=state, owner_team=owner_team)]


def require_issue(uow: UnitOfWork, issue_id: uuid.UUID) -> Issue:
    issue = uow.get_issue(issue_id)
    if issue is None:
        raise DomainError(ErrorCode.NOT_FOUND, "issue not found", {"issue_id": str(issue_id)})
    return issue


def issue_detail(uow: UnitOfWork, issue_id: uuid.UUID) -> IssueDetail:
    issue = require_issue(uow, issue_id)
    revisions = uow.list_revisions(issue.id)
    latest = revisions[-1] if revisions else None
    sessions = list(uow.list_sessions(issue_id=issue.id))
    return IssueDetail(
        issue=issue_summary(uow, issue),
        labels=list(latest.labels) if latest else [],
        target_commit=latest.target_commit if latest else None,
        superset_version=latest.superset_version if latest else None,
        questions=list(uow.list_questions(issue.id, None)),
        evidence=list(uow.list_evidence(issue.id)),
        decisions=list(uow.list_decisions(issue.id)),
        pull_requests=list(uow.list_pull_requests(issue.id)),
        sessions=[SessionSummary.from_session(s, issue.key) for s in sessions],
        jobs=list(uow.list_jobs(issue.id)),
        attempts=list(uow.list_attempts(issue.id)),
    )


def list_sessions(
    uow: UnitOfWork, *, issue_id: uuid.UUID | None, state: str | None
) -> list[SessionSummary]:
    keys: dict[uuid.UUID, str] = {}
    out: list[SessionSummary] = []
    for session in uow.list_sessions(issue_id=issue_id):
        if state is not None and session.state.value != state:
            continue
        if session.issue_id not in keys:
            issue = uow.get_issue(session.issue_id)
            keys[session.issue_id] = issue.key if issue else "SUP-?"
        out.append(SessionSummary.from_session(session, keys[session.issue_id]))
    return out


def session_detail(uow: UnitOfWork, session_id: uuid.UUID) -> SessionDetail:
    session = uow.get_session(session_id)
    if session is None:
        raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": str(session_id)})
    issue = uow.get_issue(session.issue_id)
    key = issue.key if issue else "SUP-?"
    return SessionDetail(
        session=SessionSummary.from_session(session, key),
        budget=session.budget.model_dump(),
        timeline=list(uow.list_session_events(session.id)),
        conversation=list(uow.list_messages(session.id)),
        outputs=list(uow.list_outputs(session.id)),
        evidence=[e for e in uow.list_evidence(session.issue_id) if e.session_id == session.id],
    )


def workflow() -> WorkflowResponse:
    return WorkflowResponse(
        version=WORKFLOW_VERSION,
        repository=TARGET_REPOSITORY,
        states=list(IssueState),
        terminal_states=sorted(TERMINAL_STATES, key=lambda s: s.value),
        waiting_states=sorted(WAITING_STATES, key=lambda s: s.value),
        active_agent_states=sorted(ACTIVE_AGENT_STATES, key=lambda s: s.value),
        transitions=[
            WorkflowTransition(
                name=spec.name,
                events=sorted(e.value for e in spec.events),
                sources=sorted(spec.sources, key=lambda s: s.value),
                destinations=sorted(spec.destinations, key=lambda s: s.value),
                actors=sorted(spec.actors, key=lambda a: a.value),
                human_gate=spec.human_gate,
                description=spec.description,
                preconditions=list(spec.preconditions),
                public_side_effects=list(spec.public_side_effects),
            )
            for spec in TRANSITIONS.values()
        ],
    )


def analytics(uow: UnitOfWork) -> AnalyticsSummary:
    issues = list(uow.list_issues())
    sessions = list(uow.list_sessions())
    attempts = list(uow.list_all_attempts())
    jobs = list(uow.list_jobs())
    open_questions = 0
    prs_open = 0
    prs_approved = 0
    for issue in issues:
        open_questions += sum(
            1
            for q in uow.list_questions(issue.id, issue.revision)
            if q.required and q.status == QuestionStatus.OPEN
        )
        for pr in uow.list_pull_requests(issue.id):
            if not pr.merged:
                prs_open += 1
            if pr.human_approved:
                prs_approved += 1
    status_counts = Counter(a.status for a in attempts)
    return AnalyticsSummary(
        repository=repository_name(uow),
        issues_total=len(issues),
        issues_by_state=dict(sorted(Counter(i.state.value for i in issues).items())),
        sessions_total=len(sessions),
        sessions_by_state=dict(sorted(Counter(s.state.value for s in sessions).items())),
        live_workspaces=sum(1 for s in sessions if s.workspace_live),
        open_questions=open_questions,
        pending_jobs=sum(1 for j in jobs if j.status == JobStatus.PENDING),
        attempts_applied=status_counts[AttemptStatus.APPLIED],
        attempts_rejected=status_counts[AttemptStatus.REJECTED],
        attempts_noop=status_counts[AttemptStatus.NOOP],
        rejections_by_code=dict(
            sorted(Counter(a.error_code or "unknown" for a in attempts if a.error_code).items())
        ),
        pull_requests_open=prs_open,
        pull_requests_human_approved=prs_approved,
        security_private=sum(1 for i in issues if i.state == IssueState.SECURITY_PRIVATE),
    )
