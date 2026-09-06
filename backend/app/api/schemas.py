"""Request/response schemas for /api/v1.

Response shapes mirror the dashboard contract in PR #8 (`src/api/types.ts`).
Live-session values that Relay has not explicitly synchronized from an
approved source (progress, current action, next checkpoint, conversation,
desktop URL) are exposed as `null`, never as 0/empty-string placeholders.
Command bodies never carry actor identity; see `app.api.deps`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.states import ActorRole, DecisionKind, IssueState


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------- errors


class ErrorBody(ApiModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(ApiModel):
    error: ErrorBody


# ------------------------------------------------------------------- health


class HealthResponse(ApiModel):
    status: Literal["ok", "degraded"]
    version: str
    workflow_version: str
    repository: str


class ReadyResponse(ApiModel):
    database: Literal["ok", "unavailable"]
    worker: Literal["ok", "stale", "unavailable"]
    last_worker_heartbeat_at: datetime | None = None
    migrations: str
    dry_run: bool


# ------------------------------------------------------------- shared parts


class RepositoryRef(ApiModel):
    full_name: str
    html_url: str
    default_branch: str | None = None
    dry_run: bool


class ActorRef(ApiModel):
    kind: ActorRole
    display_name: str
    login: str | None = None


class LifecycleEvent(ApiModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    correlation_id: str
    occurred_at: datetime
    kind: str
    summary: str
    detail: str | None = None
    from_state: IssueState | None = None
    to_state: IssueState | None = None
    actor: ActorRef | None = None
    session_id: uuid.UUID | None = None
    outcome: Literal["accepted", "rejected", "duplicate", "pending", "failed"]


class InformationRequestOut(ApiModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    issue_revision: int
    field: str
    prompt: str
    rationale: str
    safe_example: str | None = None
    prohibited_data: list[str]
    required: bool
    status: str
    answer: str | None = None
    answered_at: datetime | None = None
    reminder_due_at: datetime | None = None
    inactivity_close_at: datetime | None = None


class EvidenceItem(ApiModel):
    id: uuid.UUID
    label: str
    value: str
    status: Literal["complete", "draft", "missing", "invalid"]
    artifact_id: uuid.UUID | None = None


class EvidencePacket(ApiModel):
    completeness: float
    reproduction_ready: bool
    items: list[EvidenceItem]
    target_version: str | None = None
    security_classification: Literal["none", "suspected", "confirmed_private"]


class AuthorizationRef(ApiModel):
    scope: str
    expires_at: datetime


class HumanDecisionOut(ApiModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    issue_revision: int
    kind: DecisionKind
    actor: ActorRef
    decided_at: datetime
    rationale: str | None = None
    superseded_by: uuid.UUID | None = None
    authorization: AuthorizationRef | None = None


class OwnerCandidate(ApiModel):
    team: str
    rule: str
    rationale: str
    paths: list[str] = Field(default_factory=list)
    selected: bool
    review_requested: Literal["accepted", "rejected", "pending", "not_requested"] | None = None


class OwnerRouting(ApiModel):
    state: Literal["resolved", "ambiguous", "no_owner"]
    candidates: list[OwnerCandidate]
    escalation: str | None = None
    pull_request_number: int | None = None
    head_sha: str | None = None
    unowned_paths: list[str] = Field(default_factory=list)
    ambiguous_paths: list[str] = Field(default_factory=list)
    source: str | None = None


class HumanGate(ApiModel):
    kind: Literal["reporter", "owner", "security", "operator", "none"]
    waiting_since: datetime | None = None
    due_at: datetime | None = None
    workspace_released: bool
    escalation: str | None = None


class PullRequestRef(ApiModel):
    repository: str
    number: int
    title: str | None = None
    html_url: str
    head_branch: str
    base_branch: str
    head_sha: str
    draft: bool
    state: Literal["open", "closed", "merged"]
    checks: Literal["pending", "passed", "failed", "unknown"]
    review: Literal["none", "requested", "approved", "changes_requested"]
    human_approver: str | None = None
    approved_head_sha: str | None = None
    approval_override: bool = False


class ReviewFinding(ApiModel):
    id: uuid.UUID
    severity: Literal["low", "medium", "high", "critical"]
    title: str
    path: str | None = None
    line: int | None = None
    detail: str | None = None


class DevinReviewState(ApiModel):
    status: Literal["not_requested", "queued", "running", "completed", "failed"]
    pull_request_number: int | None = None
    head_sha: str | None = None
    url: str | None = None
    verdict: str | None = None
    findings: list[ReviewFinding]
    findings_count: int | None = None
    completed_at: datetime | None = None


# -------------------------------------------------------------------- issues


class IssueSummary(ApiModel):
    id: uuid.UUID
    version: int
    repository: RepositoryRef
    external_number: int
    key: str
    title: str
    html_url: str
    reporter: ActorRef
    state: IssueState
    category: str | None = None
    opened_at: datetime
    updated_at: datetime
    owner_routing: OwnerRouting
    confidence: float | None = None
    progress: int
    next_action: str
    next_action_due: datetime | None = None
    missing_fields: list[str]
    human_gate: HumanGate
    security_flagged: bool
    workspace_live: bool


class IssueDetail(IssueSummary):
    revision: int
    body_excerpt: str | None = None
    labels: list[str]
    target_commit: str | None = None
    events: list[LifecycleEvent]
    questions: list[InformationRequestOut]
    evidence: EvidencePacket
    decisions: list[HumanDecisionOut]
    session_ids: list[uuid.UUID]
    pull_requests: list[PullRequestRef]


# ------------------------------------------------------------------ sessions


class ConversationAttachment(ApiModel):
    id: uuid.UUID
    name: str
    content_type: str
    size_bytes: int
    url: str | None = None


class ConversationMessageOut(ApiModel):
    id: uuid.UUID
    author: ActorRef
    sent_at: datetime
    body: str
    attachments: list[ConversationAttachment]


class SessionEventOut(ApiModel):
    id: uuid.UUID
    occurred_at: datetime
    label: str
    detail: str | None = None
    state: Literal["complete", "active", "pending", "blocked"]


class SessionOutputOut(ApiModel):
    id: uuid.UUID
    schema_name: str = Field(serialization_alias="schema")
    label: str
    summary: str | None = None
    produced_at: datetime
    accepted: bool


class Artifact(ApiModel):
    id: uuid.UUID
    kind: str
    label: str
    content_type: str
    size_bytes: int
    url: str | None = None
    retained: bool


class SessionBudgetOut(ApiModel):
    wall_seconds: int
    retry_limit: int
    retries_used: int
    allowed_capabilities: list[str]
    max_output_bytes: int


class SessionLinks(ApiModel):
    devin_session_url: str | None = None
    devin_desktop_url: str | None = None
    conversation_embeddable: bool
    desktop_embeddable: bool


class WorkspaceRef(ApiModel):
    id: str | None = None
    released: bool
    released_at: datetime | None = None


class SessionSummary(ApiModel):
    id: uuid.UUID
    version: int
    issue_id: uuid.UUID
    issue_key: str
    issue_title: str
    title: str
    kind: str
    transition: str
    actor: str
    status: str
    dry_run: bool
    created_at: datetime
    started_at: datetime | None = None
    ended_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    progress: int | None = None
    progress_source: str | None = None
    progress_synced_at: datetime | None = None
    budget: SessionBudgetOut
    repository: RepositoryRef
    target_commit: str | None = None
    branch: str | None = None
    workspace: WorkspaceRef
    trigger: str
    cancel_requested: bool
    current_action: str | None = None
    next_checkpoint: str | None = None
    correlation_id: str
    human_gate: HumanGate


class SessionDetail(SessionSummary):
    conversation: list[ConversationMessageOut] | None
    events: list[SessionEventOut]
    outputs: list[SessionOutputOut]
    artifacts: list[Artifact]
    pull_requests: list[PullRequestRef]
    review: DevinReviewState
    links: SessionLinks


# ------------------------------------------------------------------- listing


class IssuePage(ApiModel):
    items: list[IssueSummary]
    total: int
    generated_at: datetime


class SessionPage(ApiModel):
    items: list[SessionSummary]
    total: int
    generated_at: datetime


# ----------------------------------------------------------------- workflow


class WorkflowStep(ApiModel):
    id: str
    label: str
    kind: Literal["automation", "ai", "human", "terminal"]
    states: list[IssueState]
    sla: str | None = None
    actor: str


class WorkflowTransitionOut(ApiModel):
    name: str
    events: list[str]
    sources: list[IssueState]
    destinations: list[IssueState]
    actors: list[ActorRole]
    human_gate: bool
    description: str
    preconditions: list[str]
    public_side_effects: list[str]


class WorkflowDefinition(ApiModel):
    version: str
    status: Literal["draft", "active"]
    repository: str
    steps: list[WorkflowStep]
    transitions: list[WorkflowTransitionOut]
    terminal_states: list[IssueState]
    waiting_states: list[IssueState]
    active_agent_states: list[IssueState]
    updated_at: datetime


# ---------------------------------------------------------------- analytics


class Period(ApiModel):
    from_: datetime = Field(serialization_alias="from")
    to: datetime


class OutcomeSlice(ApiModel):
    label: str
    value: int


class OwnerLoad(ApiModel):
    owner: str
    initials: str
    active: int
    waiting: int
    sla: int


class AnalyticsSummary(ApiModel):
    period: Period
    issues_processed: int
    confirmed_bugs: int
    reproduced_autonomously_pct: float
    median_to_owner_decision_hours: float | None = None
    state_counts: dict[str, int]
    outcome_mix: list[OutcomeSlice]
    owner_load: list[OwnerLoad]
    sessions_total: int
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
    generated_at: datetime


# ------------------------------------------------------------------ commands


class DryRunRequest(ApiModel):
    """Seeds a synthetic issue in dry-run mode. Text is stored, never executed."""

    scenario: str | None = Field(default=None, max_length=100)
    number: int | None = Field(default=None, ge=1)
    title: str = Field(default="Dry-run issue", max_length=500)
    body: str = Field(default="", max_length=20_000)
    labels: list[str] = Field(default_factory=list)
    reporter: str = Field(default="dry-run-reporter", max_length=100)
    category: str = Field(default="Uncategorized", max_length=100)
    owner_team: str | None = Field(default=None, max_length=100)


class ReporterAnswer(ApiModel):
    field: str = Field(min_length=1, max_length=100)
    answer: str | None = Field(default=None, max_length=20_000)
    unavailable: bool = False


class AnswerResponse(ApiModel):
    kind: Literal["answer"]
    value: str = Field(min_length=1, max_length=20_000)


class UnavailableResponse(ApiModel):
    kind: Literal["unavailable"]
    reason: str | None = Field(default=None, max_length=2_000)


class ReporterResponseRequest(ApiModel):
    """PR #8 shape (`question_id` + `response`) or a batch of `answers`."""

    issue_revision: int | None = Field(default=None, ge=1)
    question_id: uuid.UUID | None = None
    response: AnswerResponse | UnavailableResponse | None = None
    answers: list[ReporterAnswer] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _one_form(self) -> ReporterResponseRequest:
        single = self.question_id is not None or self.response is not None
        if single and (self.question_id is None or self.response is None):
            raise ValueError("question_id and response must be provided together")
        if single == (self.answers is not None):
            raise ValueError("provide either question_id+response or answers")
        return self


ReclassifyAs = Literal["duplicate", "not_a_bug", "unsupported", "support"]


class OwnerDecisionRequest(ApiModel):
    issue_revision: int | None = Field(default=None, ge=1)
    kind: DecisionKind
    rationale: str = Field(default="", max_length=5_000)
    reclassify_as: ReclassifyAs | None = None
    scope: str | None = Field(default=None, max_length=1_000)
    field: str | None = Field(default=None, max_length=100)
    prompt: str | None = Field(default=None, max_length=2_000)
    pr_number: int | None = Field(default=None, ge=1)
    head_sha: str | None = Field(default=None, min_length=7, max_length=64)


class RetryRequest(ApiModel):
    reason: str = Field(default="operator retry", max_length=1_000)


class SessionCancelRequest(ApiModel):
    reason: str = Field(default="operator cancel", max_length=1_000)


class SessionMessageRequest(ApiModel):
    body: str = Field(min_length=1, max_length=10_000)


class CommandAccepted(ApiModel):
    event_id: uuid.UUID
    resource_id: uuid.UUID
    resource_version: int
    accepted_at: datetime
    processing: Literal["complete", "async"]
    duplicate: bool
    status: str
    transition: str | None = None
    from_state: IssueState | None = None
    to_state: IssueState | None = None
    detail: str
    scenario: str | None = None
    applied: int | None = None
    rejected: int | None = None
