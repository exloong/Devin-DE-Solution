"""Request/response schemas for /api/v1. Domain records are exposed directly
where they are already redacted (no issue bodies or attachment content)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    AgentSession,
    ConversationMessage,
    Evidence,
    HumanDecision,
    InformationRequest,
    Issue,
    Job,
    PullRequestState,
    SessionEvent,
    SessionOutput,
    TransitionAttempt,
)
from app.domain.states import ActorRole, DecisionKind, IssueState, TransitionName


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody


class HealthResponse(ApiModel):
    status: str
    workflow_version: str
    repository: str


class ReadyResponse(ApiModel):
    status: str
    database: str
    migrations: str
    dry_run: bool


class IssueSummary(ApiModel):
    id: uuid.UUID
    key: str
    repository: str
    external_number: int
    title: str
    reporter_login: str
    state: IssueState
    version: int
    revision: int
    category: str
    owner_team: str | None
    security_flagged: bool
    workspace_live: bool
    open_questions: int
    updated_at: datetime

    @classmethod
    def from_issue(
        cls,
        issue: Issue,
        *,
        repository: str,
        sessions: list[AgentSession],
        questions: list[InformationRequest],
    ) -> IssueSummary:
        return cls(
            id=issue.id,
            key=issue.key,
            repository=repository,
            external_number=issue.external_number,
            title=issue.title,
            reporter_login=issue.reporter_login,
            state=issue.state,
            version=issue.version,
            revision=issue.revision,
            category=issue.category,
            owner_team=issue.owner_team,
            security_flagged=issue.security_flagged,
            workspace_live=any(s.workspace_live for s in sessions),
            open_questions=sum(1 for q in questions if q.required and q.status.value == "open"),
            updated_at=issue.updated_at,
        )


class IssueListResponse(ApiModel):
    items: list[IssueSummary]
    total: int


class SessionSummary(ApiModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    issue_key: str
    issue_revision: int
    kind: str
    title: str
    state: str
    repository: str
    target_commit: str | None
    branch: str | None
    workspace_live: bool
    workspace_name: str | None
    progress_percent: int
    current_action: str
    next_checkpoint: str
    trigger: str
    cancel_requested: bool
    started_at: datetime | None
    last_heartbeat_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_session(cls, session: AgentSession, issue_key: str) -> SessionSummary:
        return cls(
            id=session.id,
            issue_id=session.issue_id,
            issue_key=issue_key,
            issue_revision=session.issue_revision,
            kind=session.kind.value,
            title=session.title,
            state=session.state.value,
            repository=session.repository,
            target_commit=session.target_commit,
            branch=session.branch,
            workspace_live=session.workspace_live,
            workspace_name=session.workspace_name if session.workspace_live else None,
            progress_percent=session.progress_percent,
            current_action=session.current_action,
            next_checkpoint=session.next_checkpoint,
            trigger=session.trigger,
            cancel_requested=session.cancel_requested,
            started_at=session.started_at,
            last_heartbeat_at=session.last_heartbeat_at,
            finished_at=session.finished_at,
        )


class SessionListResponse(ApiModel):
    items: list[SessionSummary]
    total: int


class SessionDetail(ApiModel):
    session: SessionSummary
    budget: dict[str, object]
    timeline: list[SessionEvent]
    conversation: list[ConversationMessage]
    outputs: list[SessionOutput]
    evidence: list[Evidence]


class IssueDetail(ApiModel):
    issue: IssueSummary
    labels: list[str]
    target_commit: str | None
    superset_version: str | None
    questions: list[InformationRequest]
    evidence: list[Evidence]
    decisions: list[HumanDecision]
    pull_requests: list[PullRequestState]
    sessions: list[SessionSummary]
    jobs: list[Job]
    attempts: list[TransitionAttempt]


class WorkflowTransition(ApiModel):
    name: TransitionName
    events: list[str]
    sources: list[IssueState]
    destinations: list[IssueState]
    actors: list[ActorRole]
    human_gate: bool
    description: str
    preconditions: list[str]
    public_side_effects: list[str]


class WorkflowResponse(ApiModel):
    version: str
    repository: str
    states: list[IssueState]
    terminal_states: list[IssueState]
    waiting_states: list[IssueState]
    active_agent_states: list[IssueState]
    transitions: list[WorkflowTransition]


class AnalyticsSummary(ApiModel):
    repository: str
    issues_total: int
    issues_by_state: dict[str, int]
    sessions_total: int
    sessions_by_state: dict[str, int]
    live_workspaces: int
    open_questions: int
    pending_jobs: int
    attempts_applied: int
    attempts_rejected: int
    attempts_noop: int
    rejections_by_code: dict[str, int]
    pull_requests_open: int
    pull_requests_human_approved: int
    security_private: int


# ------------------------------------------------------------------ commands


class CommandRequest(ApiModel):
    idempotency_key: str = Field(min_length=1, max_length=200)
    actor_login: str = Field(default="operator", min_length=1, max_length=100)
    expected_version: int | None = None


class DryRunRequest(CommandRequest):
    """Seeds a synthetic issue in dry-run mode. Text is stored, never executed."""

    scenario: str | None = None
    number: int | None = Field(default=None, ge=1)
    title: str = Field(default="Dry-run issue", max_length=500)
    body: str = Field(default="", max_length=20_000)
    labels: list[str] = Field(default_factory=list)
    reporter: str = "dry-run-reporter"
    category: str = "Uncategorized"
    owner_team: str | None = None


class ReporterAnswer(ApiModel):
    field: str = Field(min_length=1, max_length=100)
    answer: str | None = Field(default=None, max_length=20_000)
    unavailable: bool = False


class ReporterResponseRequest(CommandRequest):
    actor_login: str = Field(default="reporter", min_length=1, max_length=100)
    answers: list[ReporterAnswer] = Field(min_length=1)


class OwnerDecisionRequest(CommandRequest):
    actor_login: str = Field(default="owner", min_length=1, max_length=100)
    actor_role: ActorRole = ActorRole.OWNER
    decision: DecisionKind
    rationale: str = Field(default="", max_length=5_000)
    scope: str | None = Field(default=None, max_length=1_000)
    reclassify_to: IssueState | None = None
    field: str | None = Field(default=None, max_length=100)
    prompt: str | None = Field(default=None, max_length=2_000)


class RetryRequest(CommandRequest):
    reason: str = Field(default="operator retry", max_length=1_000)


class SessionCancelRequest(CommandRequest):
    reason: str = Field(default="operator cancel", max_length=1_000)


class SessionMessageRequest(CommandRequest):
    body: str = Field(min_length=1, max_length=10_000)


class DryRunResponse(ApiModel):
    scenario: str | None
    issue: IssueSummary | None
    applied: int
    duplicates: int
    rejected: int


class CommandResponse(ApiModel):
    event_id: uuid.UUID
    duplicate: bool
    status: str
    transition: TransitionName | None
    from_state: IssueState
    to_state: IssueState | None
    detail: str
    issue: IssueSummary | None
