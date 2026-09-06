"""Read-side assembly of API responses from the unit of work.

Everything here is derived from persisted Relay facts. Values that would have
to be guessed for live sessions are left `None`.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from statistics import median
from typing import Literal

from app.api.schemas import (
    ActorRef,
    AnalyticsSummary,
    Artifact,
    AuthorizationRef,
    ConversationMessageOut,
    DevinReviewState,
    EvidenceItem,
    EvidencePacket,
    HumanDecisionOut,
    HumanGate,
    InformationRequestOut,
    IssueDetail,
    IssueSummary,
    LifecycleEvent,
    OutcomeSlice,
    OwnerCandidate,
    OwnerLoad,
    OwnerRouting,
    Period,
    PullRequestRef,
    RepositoryRef,
    SessionBudgetOut,
    SessionDetail,
    SessionEventOut,
    SessionLinks,
    SessionOutputOut,
    SessionSummary,
    WorkflowDefinition,
    WorkflowStep,
    WorkflowTransitionOut,
    WorkspaceRef,
)
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import (
    Actor,
    AgentSession,
    AttemptStatus,
    ConversationAuthor,
    Event,
    HumanDecision,
    Issue,
    JobStatus,
    PullRequestState,
    QuestionStatus,
    Repository,
    ReviewerRouting,
    ReviewVerdict,
    TransitionAttempt,
)
from app.domain.ports import UnitOfWork
from app.domain.states import (
    ACTIVE_AGENT_STATES,
    TARGET_REPOSITORY,
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    WORKFLOW_VERSION,
    ActorRole,
    DecisionKind,
    IssueState,
    SessionKind,
    SessionState,
)

GITHUB = "https://github.com"

StepKind = Literal["automation", "ai", "human", "terminal"]
GateKind = Literal["reporter", "owner", "security", "operator", "none"]
ReviewLabel = Literal["none", "requested", "approved", "changes_requested"]
ChecksLabel = Literal["pending", "passed", "failed", "unknown"]
EventState = Literal["complete", "active", "pending", "blocked"]
Outcome = Literal["accepted", "rejected", "duplicate", "pending", "failed"]
RoutingLabel = Literal["resolved", "ambiguous", "no_owner"]

_WORKFLOW_STEPS: tuple[tuple[str, str, StepKind, tuple[IssueState, ...], str], ...] = (
    ("intake", "Intake & triage", "automation", (IssueState.NEW, IssueState.TRIAGE), "relay"),
    ("clarify", "Reporter clarification", "human", (IssueState.AWAITING_REPORTER,), "reporter"),
    (
        "reproduce",
        "Bounded reproduction",
        "ai",
        (IssueState.REPRODUCING, IssueState.BLOCKED_ENVIRONMENT),
        "devin",
    ),
    ("confirm", "Owner bug confirmation", "human", (IssueState.NEEDS_OWNER_DECISION,), "owner"),
    ("fix", "Authorized fix", "ai", (IssueState.FIX_AUTHORIZED, IssueState.FIXING), "devin"),
    (
        "review",
        "Review & merge approval",
        "human",
        (IssueState.PR_OPEN, IssueState.AWAITING_OWNER, IssueState.CHANGES_REQUESTED),
        "owner",
    ),
    (
        "done",
        "Terminal outcome",
        "terminal",
        (
            IssueState.COMPLETED,
            IssueState.DUPLICATE,
            IssueState.NOT_A_BUG,
            IssueState.UNSUPPORTED,
            IssueState.CLOSED_INACTIVE,
            IssueState.SECURITY_PRIVATE,
            IssueState.AUTOMATION_ERROR,
        ),
        "relay",
    ),
)

_NEXT_ACTION: dict[IssueState, str] = {
    IssueState.NEW: "Record intake",
    IssueState.TRIAGE: "Classify and check reproduction evidence",
    IssueState.AWAITING_REPORTER: "Waiting for reporter answers",
    IssueState.REPRODUCING: "Bounded reproduction session running",
    IssueState.BLOCKED_ENVIRONMENT: "Operator: unblock environment and retry",
    IssueState.NEEDS_OWNER_DECISION: "Owner: confirm bug or reclassify",
    IssueState.FIX_AUTHORIZED: "Start bounded fix session",
    IssueState.FIXING: "Bounded fix session running",
    IssueState.PR_OPEN: "Waiting for Devin Review and reviewer routing",
    IssueState.AWAITING_OWNER: "Owner: review and approve or request changes",
    IssueState.CHANGES_REQUESTED: "Resume fix with requested changes",
    IssueState.COMPLETED: "None (merged)",
    IssueState.DUPLICATE: "None (duplicate)",
    IssueState.NOT_A_BUG: "None (not a bug)",
    IssueState.UNSUPPORTED: "None (unsupported)",
    IssueState.CLOSED_INACTIVE: "Reopen with the missing evidence",
    IssueState.SECURITY_PRIVATE: "Security team handles privately",
    IssueState.AUTOMATION_ERROR: "Operator: inspect and retry",
}


def _step_index(state: IssueState) -> int:
    for index, (_, _, _, states, _) in enumerate(_WORKFLOW_STEPS):
        if state in states:
            return index
    return 0


def _lifecycle_progress(state: IssueState) -> int:
    """Position in the Relay workflow (0-100); a Relay fact, not Devin progress."""
    if state in TERMINAL_STATES:
        return 100
    return round(100 * _step_index(state) / (len(_WORKFLOW_STEPS) - 1))


def _actor_ref(actor: Actor) -> ActorRef:
    return ActorRef(kind=actor.role, display_name=actor.login, login=actor.login)


def _repository_ref(repo: Repository | None, *, dry_run: bool) -> RepositoryRef:
    name = repo.full_name if repo else TARGET_REPOSITORY
    return RepositoryRef(
        full_name=name,
        html_url=f"{GITHUB}/{name}",
        default_branch=repo.default_branch if repo else None,
        dry_run=repo.dry_run if repo else dry_run,
    )


def _gate_kind(state: IssueState) -> GateKind:
    if state == IssueState.AWAITING_REPORTER:
        return "reporter"
    if state in (
        IssueState.NEEDS_OWNER_DECISION,
        IssueState.AWAITING_OWNER,
        IssueState.CHANGES_REQUESTED,
    ):
        return "owner"
    if state == IssueState.SECURITY_PRIVATE:
        return "security"
    if state in (IssueState.BLOCKED_ENVIRONMENT, IssueState.AUTOMATION_ERROR):
        return "operator"
    return "none"


def _human_gate(issue: Issue, sessions: Sequence[AgentSession]) -> HumanGate:
    kind = _gate_kind(issue.state)
    released = not any(s.workspace_live for s in sessions)
    if kind == "none":
        return HumanGate(kind=kind, workspace_released=released)
    due_at: datetime | None = None
    waiting_since = issue.updated_at
    if kind == "reporter" and issue.reporter_wait is not None:
        waiting_since = issue.reporter_wait.started_at
        due_at = issue.reporter_wait.reminder_due_at or issue.reporter_wait.inactivity_due_at
    return HumanGate(
        kind=kind,
        waiting_since=waiting_since,
        due_at=due_at,
        workspace_released=released,
        escalation="operator retry required" if kind == "operator" else None,
    )


def _routing_label(routing: ReviewerRouting) -> RoutingLabel:
    if routing.state.value == "resolved":
        return "resolved"
    if routing.state.value == "ambiguous":
        return "ambiguous"
    return "no_owner"


def _owner_routing(issue: Issue, routings: Sequence[ReviewerRouting]) -> OwnerRouting:
    if routings:
        latest = routings[-1]
        candidates = [
            OwnerCandidate(
                team=c.team,
                rule=c.rule,
                rationale=c.rationale or latest.rationale,
                paths=list(c.paths),
                selected=c.selected,
                review_requested="pending" if c.selected else "not_requested",
            )
            for c in latest.candidates
        ]
        state = _routing_label(latest)
        escalation = None
        if state != "resolved":
            escalation = "no CODEOWNERS match; operator escalation queued"
        return OwnerRouting(
            state=state,
            candidates=candidates,
            escalation=escalation,
            pull_request_number=latest.pull_request_number,
            head_sha=latest.head_sha,
            unowned_paths=list(latest.unowned_paths),
            ambiguous_paths=list(latest.ambiguous_paths),
            source=latest.source,
        )
    if issue.owner_team:
        return OwnerRouting(
            state="resolved",
            candidates=[
                OwnerCandidate(
                    team=issue.owner_team,
                    rule="issue classification",
                    rationale="Owner team recorded at classification",
                    selected=True,
                )
            ],
            source="classification",
        )
    return OwnerRouting(state="no_owner", candidates=[])


def _missing_fields(uow: UnitOfWork, issue: Issue) -> list[str]:
    return [
        q.field
        for q in uow.list_questions(issue.id, issue.revision)
        if q.required and q.status != QuestionStatus.ANSWERED
    ]


def _issue_summary(uow: UnitOfWork, issue: Issue, repo: RepositoryRef) -> IssueSummary:
    sessions = list(uow.list_sessions(issue_id=issue.id))
    return IssueSummary(
        id=issue.id,
        version=issue.version,
        repository=repo,
        external_number=issue.external_number,
        key=issue.key,
        title=issue.title,
        html_url=f"{GITHUB}/{repo.full_name}/issues/{issue.external_number}",
        reporter=ActorRef(
            kind=ActorRole.REPORTER, display_name=issue.reporter_login, login=issue.reporter_login
        ),
        state=issue.state,
        category=issue.category,
        opened_at=issue.created_at,
        updated_at=issue.updated_at,
        owner_routing=_owner_routing(issue, uow.list_reviewer_routings(issue.id)),
        progress=_lifecycle_progress(issue.state),
        next_action=_NEXT_ACTION[issue.state],
        next_action_due=(
            issue.reporter_wait.inactivity_due_at
            if issue.state == IssueState.AWAITING_REPORTER and issue.reporter_wait
            else None
        ),
        missing_fields=_missing_fields(uow, issue),
        human_gate=_human_gate(issue, sessions),
        security_flagged=issue.security_flagged,
        workspace_live=any(s.workspace_live for s in sessions),
    )


def repository_ref(uow: UnitOfWork, *, dry_run: bool) -> RepositoryRef:
    return _repository_ref(uow.get_repository(), dry_run=dry_run)


def issue_summary(uow: UnitOfWork, issue: Issue, *, dry_run: bool) -> IssueSummary:
    return _issue_summary(uow, issue, repository_ref(uow, dry_run=dry_run))


def list_issues(
    uow: UnitOfWork,
    *,
    states: Sequence[IssueState],
    owner: str | None,
    search: str | None,
    dry_run: bool,
) -> list[IssueSummary]:
    repo = repository_ref(uow, dry_run=dry_run)
    needle = search.lower() if search else None
    out: list[IssueSummary] = []
    for issue in uow.list_issues(owner_team=owner):
        if states and issue.state not in states:
            continue
        if needle and needle not in f"{issue.key} {issue.title}".lower():
            continue
        out.append(_issue_summary(uow, issue, repo))
    return out


def require_issue(uow: UnitOfWork, issue_id: uuid.UUID) -> Issue:
    issue = uow.get_issue(issue_id)
    if issue is None:
        raise DomainError(ErrorCode.NOT_FOUND, "issue not found", {"issue_id": str(issue_id)})
    return issue


def require_session(uow: UnitOfWork, session_id: uuid.UUID) -> AgentSession:
    session = uow.get_session(session_id)
    if session is None:
        raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": str(session_id)})
    return session


def _outcome(attempt: TransitionAttempt) -> Outcome:
    if attempt.status == AttemptStatus.REJECTED:
        return "rejected"
    return "accepted"


def _lifecycle_events(
    issue: Issue, attempts: Sequence[TransitionAttempt], events: Sequence[Event]
) -> list[LifecycleEvent]:
    by_id = {e.id: e for e in events}
    out: list[LifecycleEvent] = []
    for attempt in attempts:
        event = by_id.get(attempt.event_id)
        session_raw = event.payload.get("session_id") if event else None
        session_id: uuid.UUID | None = None
        if isinstance(session_raw, str):
            try:
                session_id = uuid.UUID(session_raw)
            except ValueError:
                session_id = None
        if attempt.transition is not None:
            kind = attempt.transition.value
        else:
            kind = event.type.value if event else "event"
        out.append(
            LifecycleEvent(
                id=attempt.id,
                issue_id=issue.id,
                correlation_id=attempt.correlation_id,
                occurred_at=attempt.created_at,
                kind=kind,
                summary=attempt.detail,
                detail=attempt.error_code,
                from_state=attempt.from_state,
                to_state=attempt.to_state,
                actor=_actor_ref(event.actor) if event else None,
                session_id=session_id,
                outcome=_outcome(attempt),
            )
        )
    return out


def _question_out(q_issue_id: uuid.UUID, uow: UnitOfWork) -> list[InformationRequestOut]:
    return [
        InformationRequestOut(
            id=q.id,
            issue_id=q.issue_id,
            issue_revision=q.issue_revision,
            field=q.field,
            prompt=q.prompt,
            rationale=q.why_it_matters,
            safe_example=q.safe_example or None,
            prohibited_data=[p.strip() for p in q.prohibited_data.split(",") if p.strip()],
            required=q.required,
            status=q.status.value,
            answer=q.answer,
            answered_at=q.answered_at,
            reminder_due_at=q.reminder_due_at,
            inactivity_close_at=q.inactivity_due_at,
        )
        for q in uow.list_questions(q_issue_id, None)
    ]


def _evidence_packet(uow: UnitOfWork, issue: Issue, target_version: str | None) -> EvidencePacket:
    questions = [q for q in uow.list_questions(issue.id, issue.revision) if q.required]
    answered = sum(1 for q in questions if q.status == QuestionStatus.ANSWERED)
    completeness = 1.0 if not questions else answered / len(questions)
    items = [
        EvidenceItem(
            id=e.id,
            label=e.title,
            value=e.summary,
            status="draft" if e.private else "complete",
            artifact_id=e.id,
        )
        for e in uow.list_evidence(issue.id)
        if e.issue_revision == issue.revision
    ]
    return EvidencePacket(
        completeness=round(completeness, 3),
        reproduction_ready=not issue.security_flagged and answered == len(questions),
        items=items,
        target_version=target_version,
        security_classification="confirmed_private" if issue.security_flagged else "none",
    )


def _decision_out(d: HumanDecision) -> HumanDecisionOut:
    authorization = None
    if d.scope is not None and d.expires_at is not None:
        authorization = AuthorizationRef(scope=d.scope, expires_at=d.expires_at)
    return HumanDecisionOut(
        id=d.id,
        issue_id=d.issue_id,
        issue_revision=d.issue_revision,
        kind=d.kind,
        actor=_actor_ref(d.actor),
        decided_at=d.created_at,
        rationale=d.rationale or None,
        superseded_by=d.superseded_by,
        authorization=authorization,
    )


def _checks(pr: PullRequestState) -> ChecksLabel:
    if pr.checks_passed is None:
        return "unknown"
    return "passed" if pr.checks_passed else "failed"


def _review_label(pr: PullRequestState, issue_state: IssueState) -> ReviewLabel:
    if pr.approval_binds(pr.head_sha):
        return "approved"
    if issue_state == IssueState.CHANGES_REQUESTED:
        return "changes_requested"
    if pr.routing_id is not None:
        return "requested"
    return "none"


def _pr_ref(pr: PullRequestState, issue_state: IssueState, base_branch: str) -> PullRequestRef:
    return PullRequestRef(
        repository=pr.repository,
        number=pr.number,
        title=pr.title,
        html_url=pr.url,
        head_branch=pr.head_branch,
        base_branch=base_branch,
        head_sha=pr.head_sha,
        draft=pr.draft,
        state="merged" if pr.merged else "open",
        checks=_checks(pr),
        review=_review_label(pr, issue_state),
        human_approver=pr.human_approver,
        approved_head_sha=pr.approved_head_sha,
        approval_override=pr.approval_override,
    )


def _review_state(prs: Sequence[PullRequestState]) -> DevinReviewState:
    if not prs:
        return DevinReviewState(status="not_requested", findings=[])
    pr = prs[-1]
    if pr.devin_review == ReviewVerdict.PENDING:
        return DevinReviewState(
            status="queued",
            pull_request_number=pr.number,
            head_sha=pr.head_sha,
            findings=[],
        )
    return DevinReviewState(
        status="failed" if pr.devin_review == ReviewVerdict.FAILED else "completed",
        pull_request_number=pr.number,
        head_sha=pr.devin_review_head_sha,
        url=pr.devin_review_url,
        verdict=pr.devin_review.value,
        findings=[],
        findings_count=pr.devin_review_findings,
        completed_at=pr.updated_at,
    )


def issue_detail(uow: UnitOfWork, issue_id: uuid.UUID, *, dry_run: bool) -> IssueDetail:
    issue = require_issue(uow, issue_id)
    repo = repository_ref(uow, dry_run=dry_run)
    summary = _issue_summary(uow, issue, repo)
    revisions = uow.list_revisions(issue.id)
    latest = revisions[-1] if revisions else None
    sessions = uow.list_sessions(issue_id=issue.id)
    base_branch = repo.default_branch or "master"
    return IssueDetail(
        **summary.model_dump(),
        revision=issue.revision,
        labels=list(latest.labels) if latest else [],
        target_commit=latest.target_commit if latest else None,
        events=_lifecycle_events(issue, uow.list_attempts(issue.id), uow.list_events(issue.id)),
        questions=_question_out(issue.id, uow),
        evidence=_evidence_packet(uow, issue, latest.superset_version if latest else None),
        decisions=[_decision_out(d) for d in uow.list_decisions(issue.id)],
        session_ids=[s.id for s in sessions],
        pull_requests=[
            _pr_ref(pr, issue.state, base_branch) for pr in uow.list_pull_requests(issue.id)
        ],
    )


_TRANSITION_FOR_KIND = {
    SessionKind.TRIAGE: "classify",
    SessionKind.REPRODUCTION: "start_reproduction",
    SessionKind.FIX: "start_fix",
}


def _retries_used(session: AgentSession, siblings: Sequence[AgentSession]) -> int:
    return sum(
        1
        for s in siblings
        if s.kind == session.kind
        and s.issue_revision == session.issue_revision
        and s.created_at < session.created_at
    )


def _session_summary(
    session: AgentSession,
    issue: Issue,
    siblings: Sequence[AgentSession],
    repo: RepositoryRef,
) -> SessionSummary:
    return SessionSummary(
        id=session.id,
        version=issue.version,
        issue_id=issue.id,
        issue_key=issue.key,
        issue_title=issue.title,
        title=session.title,
        kind=session.kind.value,
        transition=_TRANSITION_FOR_KIND[session.kind],
        actor="devin",
        status=session.state.value,
        dry_run=session.dry_run,
        created_at=session.created_at,
        started_at=session.started_at,
        ended_at=session.finished_at,
        last_heartbeat_at=session.last_heartbeat_at,
        progress=session.progress_percent,
        progress_source=session.progress_source,
        progress_synced_at=session.progress_synced_at,
        budget=SessionBudgetOut(
            wall_seconds=session.budget.wall_clock_seconds,
            retry_limit=session.budget.max_retries,
            retries_used=_retries_used(session, siblings),
            allowed_capabilities=list(session.budget.allowed_capabilities),
            max_output_bytes=session.budget.max_output_bytes,
        ),
        repository=repo,
        target_commit=session.target_commit,
        branch=session.branch,
        workspace=WorkspaceRef(
            id=session.workspace_name if session.workspace_live else None,
            released=session.workspace_released,
            released_at=session.workspace_released_at,
        ),
        trigger=session.trigger,
        cancel_requested=session.cancel_requested,
        current_action=session.current_action,
        next_checkpoint=session.next_checkpoint,
        correlation_id=session.correlation_id,
        human_gate=_human_gate(issue, siblings),
    )


def list_sessions(
    uow: UnitOfWork,
    *,
    issue_id: uuid.UUID | None,
    statuses: Sequence[SessionState],
    dry_run: bool,
) -> list[SessionSummary]:
    repo = repository_ref(uow, dry_run=dry_run)
    issues: dict[uuid.UUID, Issue] = {}
    siblings: dict[uuid.UUID, list[AgentSession]] = {}
    out: list[SessionSummary] = []
    for session in uow.list_sessions(issue_id=issue_id):
        if statuses and session.state not in statuses:
            continue
        if session.issue_id not in issues:
            issue = uow.get_issue(session.issue_id)
            if issue is None:
                continue
            issues[session.issue_id] = issue
            siblings[session.issue_id] = list(uow.list_sessions(issue_id=session.issue_id))
        out.append(
            _session_summary(session, issues[session.issue_id], siblings[session.issue_id], repo)
        )
    return out


def _author_ref(author: ConversationAuthor, login: str) -> ActorRef:
    role = {
        ConversationAuthor.DEVIN: ActorRole.AGENT,
        ConversationAuthor.OPERATOR: ActorRole.OPERATOR,
        ConversationAuthor.RELAY: ActorRole.SYSTEM,
    }[author]
    return ActorRef(kind=role, display_name=login, login=login)


def _event_state(raw: str) -> EventState:
    if raw == "active":
        return "active"
    if raw == "pending":
        return "pending"
    if raw == "blocked":
        return "blocked"
    return "complete"


def session_detail(uow: UnitOfWork, session_id: uuid.UUID, *, dry_run: bool) -> SessionDetail:
    session = require_session(uow, session_id)
    issue = require_issue(uow, session.issue_id)
    repo = repository_ref(uow, dry_run=dry_run)
    siblings = list(uow.list_sessions(issue_id=issue.id))
    summary = _session_summary(session, issue, siblings, repo)
    messages = uow.list_messages(session.id)
    conversation: list[ConversationMessageOut] | None = None
    if messages or session.dry_run or session.external_session_url is not None:
        conversation = [
            ConversationMessageOut(
                id=m.id,
                author=_author_ref(m.author, m.author_login),
                sent_at=m.created_at,
                body=m.body,
                attachments=[],
            )
            for m in messages
        ]
    prs = [pr for pr in uow.list_pull_requests(issue.id) if pr.session_id == session.id]
    base_branch = repo.default_branch or "master"
    return SessionDetail(
        **summary.model_dump(),
        conversation=conversation,
        events=[
            SessionEventOut(
                id=e.id,
                occurred_at=e.created_at,
                label=e.label,
                detail=e.detail or None,
                state=_event_state(e.state),
            )
            for e in uow.list_session_events(session.id)
        ],
        outputs=[
            SessionOutputOut(
                id=o.id,
                schema_name=o.schema_name,
                label=o.schema_name,
                summary=o.rejection_reason,
                produced_at=o.created_at,
                accepted=o.accepted,
            )
            for o in uow.list_outputs(session.id)
        ],
        artifacts=[
            Artifact(
                id=e.id,
                kind=e.kind.value,
                label=e.title,
                content_type=e.content_type,
                size_bytes=e.size_bytes,
                retained=True,
            )
            for e in uow.list_evidence(issue.id)
            if e.session_id == session.id
        ],
        pull_requests=[_pr_ref(pr, issue.state, base_branch) for pr in prs],
        review=_review_state(prs),
        links=SessionLinks(
            devin_session_url=session.external_session_url,
            devin_desktop_url=session.external_desktop_url,
            conversation_embeddable=session.external_session_url is not None,
            desktop_embeddable=session.external_desktop_url is not None,
        ),
    )


def workflow(now: datetime) -> WorkflowDefinition:
    steps = [
        WorkflowStep(id=step_id, label=label, kind=kind, states=list(states), actor=actor)
        for step_id, label, kind, states, actor in _WORKFLOW_STEPS
    ]
    return WorkflowDefinition(
        version=WORKFLOW_VERSION,
        status="active",
        repository=TARGET_REPOSITORY,
        steps=steps,
        transitions=[
            WorkflowTransitionOut(
                name=spec.name.value,
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
        terminal_states=sorted(TERMINAL_STATES, key=lambda s: s.value),
        waiting_states=sorted(WAITING_STATES, key=lambda s: s.value),
        active_agent_states=sorted(ACTIVE_AGENT_STATES, key=lambda s: s.value),
        updated_at=now,
    )


def _initials(name: str) -> str:
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or name[:2].upper()


def analytics(uow: UnitOfWork, now: datetime) -> AnalyticsSummary:
    issues = list(uow.list_issues())
    sessions = list(uow.list_sessions())
    attempts = list(uow.list_all_attempts())
    jobs = list(uow.list_jobs())
    open_questions = 0
    prs_open = 0
    prs_approved = 0
    confirmed = 0
    reproduced = 0
    owner_decision_hours: list[float] = []
    owner_load: dict[str, OwnerLoad] = {}
    for issue in issues:
        open_questions += sum(
            1
            for q in uow.list_questions(issue.id, issue.revision)
            if q.required and q.status == QuestionStatus.OPEN
        )
        for pr in uow.list_pull_requests(issue.id):
            if not pr.merged:
                prs_open += 1
            if pr.approval_binds(pr.head_sha):
                prs_approved += 1
        decisions = uow.list_decisions(issue.id)
        confirmations = [d for d in decisions if d.kind == DecisionKind.CONFIRM_BUG]
        if confirmations:
            confirmed += 1
            owner_decision_hours.append(
                (confirmations[0].created_at - issue.created_at).total_seconds() / 3600
            )
        if any(s.kind == SessionKind.REPRODUCTION for s in sessions if s.issue_id == issue.id) and (
            issue.state
            in (
                IssueState.NEEDS_OWNER_DECISION,
                IssueState.FIX_AUTHORIZED,
                IssueState.FIXING,
                IssueState.PR_OPEN,
                IssueState.AWAITING_OWNER,
                IssueState.CHANGES_REQUESTED,
                IssueState.COMPLETED,
            )
        ):
            reproduced += 1
        if issue.owner_team:
            load = owner_load.setdefault(
                issue.owner_team,
                OwnerLoad(
                    owner=issue.owner_team,
                    initials=_initials(issue.owner_team),
                    active=0,
                    waiting=0,
                    sla=0,
                ),
            )
            if issue.state in WAITING_STATES:
                load.waiting += 1
            elif issue.state not in TERMINAL_STATES:
                load.active += 1
    status_counts = Counter(a.status for a in attempts)
    state_counts = Counter(i.state.value for i in issues)
    earliest = min((i.created_at for i in issues), default=now)
    processed = len(issues)
    return AnalyticsSummary(
        period=Period(from_=earliest, to=now),
        issues_processed=processed,
        confirmed_bugs=confirmed,
        reproduced_autonomously_pct=round(100 * reproduced / processed, 1) if processed else 0.0,
        median_to_owner_decision_hours=(
            round(median(owner_decision_hours), 2) if owner_decision_hours else None
        ),
        state_counts=dict(sorted(state_counts.items())),
        outcome_mix=[
            OutcomeSlice(label=state.value, value=state_counts[state.value])
            for state in sorted(TERMINAL_STATES, key=lambda s: s.value)
            if state_counts[state.value]
        ],
        owner_load=sorted(owner_load.values(), key=lambda o: o.owner),
        sessions_total=len(sessions),
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
        security_private=state_counts[IssueState.SECURITY_PRIVATE.value],
        generated_at=now,
    )
