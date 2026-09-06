"""Typed lifecycle records. These are the persistence-neutral shapes the
transition service reads and writes; the API exposes them (redacted) directly."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.states import (
    TARGET_REPOSITORY,
    ActorRole,
    DecisionKind,
    EventType,
    IssueState,
    SessionKind,
    SessionState,
    TransitionName,
)

_ID_LOCK = threading.Lock()
_last_id_ms = 0
_id_counter = 0


def new_id() -> uuid.UUID:
    """Time-ordered UUID (v7 layout) so ``ORDER BY id`` is chronological."""
    global _last_id_ms, _id_counter
    with _ID_LOCK:
        ms = int(time.time() * 1000)
        if ms <= _last_id_ms:
            ms = _last_id_ms
            _id_counter += 1
        else:
            _last_id_ms = ms
            _id_counter = 0
        rand = secrets.randbits(62)
        value = (ms << 80) | (0x7 << 76) | ((_id_counter & 0xFFF) << 64) | (0b10 << 62) | rand
        return uuid.UUID(int=value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Record(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")


class Actor(Record):
    role: ActorRole
    login: str = "relay-system"


class RepositoryScope(Record):
    """The only repository Relay may read from or write to."""

    full_name: str = TARGET_REPOSITORY
    default_branch: str = "master"
    dry_run: bool = True

    def assert_allowed(self, candidate: str) -> None:
        from app.domain.errors import DomainError, ErrorCode

        if candidate != self.full_name:
            raise DomainError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "repository is outside the configured scope",
                {"repository": candidate, "allowed": self.full_name},
            )

    def pull_request_url(self, number: int) -> str:
        return f"https://github.com/{self.full_name}/pull/{number}"


class Repository(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    full_name: str = TARGET_REPOSITORY
    default_branch: str = "master"
    dry_run: bool = True
    created_at: datetime = Field(default_factory=utcnow)


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReporterWaitPolicy(Record):
    """Deterministic reminder/inactivity schedule for one reporter-wait period.

    ``policy_revision`` increments every time the issue starts waiting on the
    reporter; timer events must carry the revision and the exact ``due_at`` they
    were scheduled for, so stale or early timers cannot act.
    """

    policy_revision: int
    started_at: datetime
    reminders_sent: int = 0
    max_reminders: int = 2
    reminder_due_at: datetime | None = None
    inactivity_due_at: datetime | None = None


class Issue(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    repository_id: uuid.UUID
    external_number: int
    title: str
    reporter_login: str
    state: IssueState = IssueState.NEW
    previous_state: IssueState | None = None
    version: int = 0
    revision: int = 1
    category: str = "Uncategorized"
    owner_team: str | None = None
    security_flagged: bool = False
    reporter_wait: ReporterWaitPolicy | None = None
    correlation_id: str = Field(default_factory=lambda: str(new_id()))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> str:
        return f"SUP-{self.external_number}"


class IssueRevision(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    revision: int
    title: str
    body: str
    labels: list[str] = Field(default_factory=list)
    target_commit: str | None = None
    superset_version: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Event(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID | None
    source: str
    delivery_id: str
    type: EventType
    actor: Actor
    issue_revision: int | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: str = Field(default_factory=lambda: str(new_id()))
    received_at: datetime = Field(default_factory=utcnow)


class AttemptStatus(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    NOOP = "noop"


class TransitionAttempt(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    event_id: uuid.UUID
    issue_revision: int
    transition: TransitionName | None
    from_state: IssueState
    to_state: IssueState | None
    status: AttemptStatus
    error_code: str | None = None
    detail: str = ""
    resource_version: int
    correlation_id: str
    created_at: datetime = Field(default_factory=utcnow)


class QuestionStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


class InformationRequest(Record):
    """A single reporter question. Kept as its own record so answers persist."""

    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    issue_revision: int
    field: str
    prompt: str
    why_it_matters: str = ""
    safe_example: str = ""
    prohibited_data: str = "Credentials, customer data, private URLs"
    required: bool = True
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    reminder_due_at: datetime | None = None
    inactivity_due_at: datetime | None = None
    answered_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class EvidenceKind(str, Enum):
    REPRODUCTION_PLAN = "reproduction_plan"
    FIXTURE_MANIFEST = "fixture_manifest"
    RUN_LOG = "run_log"
    RESULT_MATRIX = "result_matrix"
    REGRESSION_TEST = "regression_test"
    PATCH = "patch"
    TEST_OUTPUT = "test_output"
    REVIEW_FINDINGS = "review_findings"
    REPORTER_SNAPSHOT = "reporter_snapshot"


class Evidence(Record):
    """Typed artifact metadata. Content is stored elsewhere; never executed."""

    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    issue_revision: int
    session_id: uuid.UUID | None = None
    kind: EvidenceKind
    title: str
    content_type: str = "text/plain"
    size_bytes: int = 0
    private: bool = False
    summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class HumanDecision(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    issue_revision: int
    kind: DecisionKind
    actor: Actor
    rationale: str = ""
    reclassify_to: IssueState | None = None
    scope: str | None = None
    expires_at: datetime | None = None
    superseded_by: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)


class JobStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    CLASSIFY = "classify"
    PUBLISH_QUESTIONS = "publish_questions"
    START_REPRODUCTION = "start_reproduction"
    START_FIX = "start_fix"
    TRIGGER_DEVIN_REVIEW = "trigger_devin_review"
    RESOLVE_REVIEWERS = "resolve_reviewers"
    REQUEST_REVIEWERS = "request_reviewers"
    REMINDER = "reminder"
    PUBLISH_REMINDER = "publish_reminder"
    INACTIVITY = "inactivity"
    ESCALATE_OWNER = "escalate_owner"
    SAFE_ALTERNATIVE_REVIEW = "safe_alternative_review"
    PRIVATE_SECURITY_TASK = "private_security_task"
    CANCEL_SESSION = "cancel_session"
    DELIVER_SESSION_MESSAGE = "deliver_session_message"
    RECOVERY = "recovery"


PUBLIC_JOB_KINDS: frozenset[JobKind] = frozenset(
    {
        JobKind.PUBLISH_QUESTIONS,
        JobKind.START_REPRODUCTION,
        JobKind.START_FIX,
        JobKind.TRIGGER_DEVIN_REVIEW,
        JobKind.REQUEST_REVIEWERS,
        JobKind.PUBLISH_REMINDER,
    }
)


class Job(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID | None
    kind: JobKind
    idempotency_key: str
    status: JobStatus = JobStatus.PENDING
    run_after: datetime = Field(default_factory=utcnow)
    attempts: int = 0
    max_attempts: int = 3
    payload: dict[str, object] = Field(default_factory=dict)
    correlation_id: str
    issue_revision: int | None = None
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


# Jobs that may only run while the issue is security-private. Everything else is
# cancelled when an issue is routed to the private security path.
PRIVATE_JOB_KINDS: frozenset[JobKind] = frozenset({JobKind.PRIVATE_SECURITY_TASK})


class SessionBudget(Record):
    wall_clock_seconds: int = 3600
    max_retries: int = 1
    allowed_capabilities: list[str] = Field(default_factory=lambda: ["read_repo"])
    max_output_bytes: int = 1_000_000


class AgentSession(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    issue_revision: int
    kind: SessionKind
    title: str
    version: int = 0
    state: SessionState = SessionState.QUEUED
    repository: str = TARGET_REPOSITORY
    target_commit: str | None = None
    branch: str | None = None
    budget: SessionBudget = Field(default_factory=SessionBudget)
    workspace_released: bool = True
    workspace_name: str | None = None
    workspace_released_at: datetime | None = None
    dry_run: bool = False
    progress_percent: int | None = None
    current_action: str | None = None
    next_checkpoint: str | None = None
    progress_source: str | None = None
    progress_synced_at: datetime | None = None
    trigger: str = ""
    cancel_requested: bool = False
    external_session_id: str | None = None
    external_session_url: str | None = None
    external_desktop_url: str | None = None
    automation_id: str | None = None
    dispatched_at: datetime | None = None
    correlation_id: str
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def workspace_live(self) -> bool:
        return self.state == SessionState.RUNNING and not self.workspace_released


class ConversationAuthor(str, Enum):
    RELAY = "relay"
    DEVIN = "devin"
    OPERATOR = "operator"


class ConversationMessage(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    session_id: uuid.UUID
    author: ConversationAuthor
    author_login: str
    body: str
    delivered: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class SessionEvent(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    session_id: uuid.UUID
    label: str
    detail: str
    state: str = "complete"
    created_at: datetime = Field(default_factory=utcnow)


class SessionOutput(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    session_id: uuid.UUID
    schema_name: str
    accepted: bool
    rejection_reason: str | None = None
    issue_revision: int
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ReviewVerdict(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FINDINGS = "findings"
    FAILED = "failed"


class PullRequestState(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    session_id: uuid.UUID | None = None
    repository: str = TARGET_REPOSITORY
    number: int
    title: str | None = None
    head_branch: str
    head_sha: str
    url: str
    draft: bool = True
    merged: bool = False
    checks_passed: bool | None = None
    devin_review: ReviewVerdict = ReviewVerdict.PENDING
    devin_review_findings: int = 0
    devin_review_head_sha: str | None = None
    devin_review_url: str | None = None
    human_approved: bool = False
    human_approver: str | None = None
    approved_head_sha: str | None = None
    approval_override: bool = False
    routing_id: uuid.UUID | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def approval_binds(self, head_sha: str) -> bool:
        return self.human_approved and self.approved_head_sha == head_sha == self.head_sha


class RoutingState(str, Enum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NO_OWNER = "no_owner"


class RoutingCandidate(Record):
    team: str
    members: list[str] = Field(default_factory=list)
    rule: str
    paths: list[str] = Field(default_factory=list)
    rationale: str = ""
    selected: bool = True


class ReviewerRouting(Record):
    """Trusted CODEOWNERS adapter outcome for one PR head. Agents never author this."""

    id: uuid.UUID = Field(default_factory=new_id)
    issue_id: uuid.UUID
    pull_request_number: int
    head_sha: str
    state: RoutingState
    candidates: list[RoutingCandidate] = Field(default_factory=list)
    unowned_paths: list[str] = Field(default_factory=list)
    ambiguous_paths: list[str] = Field(default_factory=list)
    rationale: str = ""
    source: str = ".github/CODEOWNERS"
    resolved_by: Actor
    created_at: datetime = Field(default_factory=utcnow)

    def authorized_logins(self) -> frozenset[str]:
        return frozenset(m for c in self.candidates if c.selected for m in c.members)


class AuditEntry(Record):
    id: uuid.UUID = Field(default_factory=new_id)
    actor: Actor
    action: str
    resource_type: str
    resource_id: str
    resource_version: int | None
    correlation_id: str
    detail: str = ""
    created_at: datetime = Field(default_factory=utcnow)
