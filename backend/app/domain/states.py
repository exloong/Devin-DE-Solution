"""Lifecycle states, events, and the legal transition table.

The table is data, not code paths: the transition service consults it to
validate every attempt, and `/api/v1/workflow` publishes it verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

WORKFLOW_VERSION = "relay-lifecycle.v1"
TARGET_REPOSITORY = "exloong/superset"


class IssueState(str, Enum):
    NEW = "new"
    TRIAGE = "triage"
    AWAITING_REPORTER = "awaiting_reporter"
    REPRODUCING = "reproducing"
    BLOCKED_ENVIRONMENT = "blocked_environment"
    NEEDS_OWNER_DECISION = "needs_owner_decision"
    FIX_AUTHORIZED = "fix_authorized"
    FIXING = "fixing"
    PR_OPEN = "pr_open"
    AWAITING_OWNER = "awaiting_owner"
    CHANGES_REQUESTED = "changes_requested"
    COMPLETED = "completed"
    DUPLICATE = "duplicate"
    NOT_A_BUG = "not_a_bug"
    UNSUPPORTED = "unsupported"
    CLOSED_INACTIVE = "closed_inactive"
    SECURITY_PRIVATE = "security_private"
    AUTOMATION_ERROR = "automation_error"


TERMINAL_STATES: frozenset[IssueState] = frozenset(
    {
        IssueState.COMPLETED,
        IssueState.DUPLICATE,
        IssueState.NOT_A_BUG,
        IssueState.UNSUPPORTED,
        IssueState.CLOSED_INACTIVE,
    }
)

# Issue states in which a human is being waited on. No agent workspace may be
# live while an issue sits in one of these states.
WAITING_STATES: frozenset[IssueState] = frozenset(
    {
        IssueState.AWAITING_REPORTER,
        IssueState.NEEDS_OWNER_DECISION,
        IssueState.AWAITING_OWNER,
        IssueState.CHANGES_REQUESTED,
        IssueState.PR_OPEN,
    }
)

# States in which an agent session may legitimately hold a workspace.
ACTIVE_AGENT_STATES: frozenset[IssueState] = frozenset(
    {IssueState.TRIAGE, IssueState.REPRODUCING, IssueState.FIXING}
)


class SessionState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


SESSION_TERMINAL: frozenset[SessionState] = frozenset(
    {SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED}
)


class SessionKind(str, Enum):
    TRIAGE = "triage"
    REPRODUCTION = "reproduction"
    FIX = "fix"


class EventType(str, Enum):
    # GitHub-sourced
    ISSUE_OPENED = "issue_opened"
    ISSUE_REOPENED = "issue_reopened"
    REPORTER_COMMENT = "reporter_comment"
    PR_OPENED = "pr_opened"
    PR_SYNCHRONIZED = "pr_synchronized"
    PR_MERGED = "pr_merged"
    HUMAN_REVIEW_SUBMITTED = "human_review_submitted"
    # Agent results
    CLASSIFICATION_RESULT = "classification_result"
    REPRODUCTION_RESULT = "reproduction_result"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    FIX_RESULT = "fix_result"
    DEVIN_REVIEW_COMPLETED = "devin_review_completed"
    SESSION_PROGRESS = "session_progress"
    # Human commands
    REPORTER_RESPONSE = "reporter_response"
    OWNER_DECISION = "owner_decision"
    RETRY_REQUESTED = "retry_requested"
    SESSION_CANCEL_REQUESTED = "session_cancel_requested"
    SESSION_MESSAGE = "session_message"
    DRY_RUN_CREATED = "dry_run_created"
    # Timers / internal
    REMINDER_ELAPSED = "reminder_elapsed"
    INACTIVITY_ELAPSED = "inactivity_elapsed"
    DUPLICATE_DETECTED = "duplicate_detected"
    SECURITY_SIGNAL = "security_signal"
    AUTOMATION_FAILURE = "automation_failure"
    FIX_SESSION_STARTED = "fix_session_started"
    REPRODUCTION_STARTED = "reproduction_started"
    REVIEWER_ROUTING_RESOLVED = "reviewer_routing_resolved"


class ActorRole(str, Enum):
    SYSTEM = "system"
    AGENT = "agent"
    REPORTER = "reporter"
    OWNER = "owner"
    SECURITY = "security"
    OPERATOR = "operator"


class DecisionKind(str, Enum):
    CONFIRM_BUG = "confirm_bug"
    REQUEST_DISCRIMINATOR = "request_discriminator"
    RECLASSIFY = "reclassify"
    ROUTE_SECURITY_PRIVATE = "route_security_private"
    APPROVE_PR = "approve_pr"
    REQUEST_CHANGES = "request_changes"


class TransitionName(str, Enum):
    INTAKE = "intake"
    REQUEST_INFORMATION = "request_information"
    RECORD_REPORTER_RESPONSE = "record_reporter_response"
    START_REPRODUCTION = "start_reproduction"
    REPRODUCTION_COMPLETED = "reproduction_completed"
    ENVIRONMENT_BLOCKED = "environment_blocked"
    CONFIRM_BUG = "confirm_bug"
    REQUEST_DISCRIMINATOR = "request_discriminator"
    RECLASSIFY = "reclassify"
    ROUTE_SECURITY_PRIVATE = "route_security_private"
    START_FIX = "start_fix"
    OPEN_PR = "open_pr"
    PR_HEAD_UPDATED = "pr_head_updated"
    DEVIN_REVIEW_RECORDED = "devin_review_recorded"
    OWNER_REQUESTED_CHANGES = "owner_requested_changes"
    RESUME_FIX = "resume_fix"
    OWNER_APPROVED = "owner_approved"
    COMPLETE = "complete"
    REMIND_REPORTER = "remind_reporter"
    CLOSE_INACTIVE = "close_inactive"
    MARK_DUPLICATE = "mark_duplicate"
    REOPEN = "reopen"
    AUTOMATION_FAILED = "automation_failed"
    RETRY = "retry"


@dataclass(frozen=True)
class TransitionSpec:
    name: TransitionName
    events: frozenset[EventType]
    sources: frozenset[IssueState]
    destinations: frozenset[IssueState]
    actors: frozenset[ActorRole]
    human_gate: bool = False
    description: str = ""
    preconditions: tuple[str, ...] = field(default_factory=tuple)
    public_side_effects: tuple[str, ...] = field(default_factory=tuple)

    @property
    def single_destination(self) -> IssueState:
        if len(self.destinations) != 1:
            raise ValueError(f"{self.name.value} has multiple destinations")
        return next(iter(self.destinations))


def _s(*states: IssueState) -> frozenset[IssueState]:
    return frozenset(states)


def _e(*events: EventType) -> frozenset[EventType]:
    return frozenset(events)


def _a(*actors: ActorRole) -> frozenset[ActorRole]:
    return frozenset(actors)


_NON_TERMINAL = frozenset(IssueState) - TERMINAL_STATES - {IssueState.SECURITY_PRIVATE}
_RECOVERABLE = _NON_TERMINAL - {IssueState.AUTOMATION_ERROR, IssueState.NEW}

TRANSITIONS: dict[TransitionName, TransitionSpec] = {
    spec.name: spec
    for spec in (
        TransitionSpec(
            TransitionName.INTAKE,
            _e(EventType.ISSUE_OPENED),
            _s(IssueState.NEW),
            _s(IssueState.TRIAGE, IssueState.SECURITY_PRIVATE),
            _a(ActorRole.SYSTEM),
            description="Normalize a new exloong/superset issue and classify safety.",
            preconditions=("repository is exloong/superset", "no security signal"),
        ),
        TransitionSpec(
            TransitionName.REQUEST_INFORMATION,
            _e(EventType.CLASSIFICATION_RESULT),
            _s(IssueState.TRIAGE),
            _s(IssueState.AWAITING_REPORTER),
            _a(ActorRole.AGENT, ActorRole.SYSTEM),
            description="Publish focused questions for missing reproduction facts.",
            public_side_effects=("issue_comment",),
        ),
        TransitionSpec(
            TransitionName.RECORD_REPORTER_RESPONSE,
            _e(EventType.REPORTER_RESPONSE, EventType.REPORTER_COMMENT),
            _s(IssueState.AWAITING_REPORTER),
            _s(IssueState.AWAITING_REPORTER, IssueState.TRIAGE),
            _a(ActorRole.REPORTER, ActorRole.OPERATOR),
            description="Store answers or unavailable markers without executing content.",
        ),
        TransitionSpec(
            TransitionName.START_REPRODUCTION,
            _e(
                EventType.REPRODUCTION_STARTED,
                EventType.CLASSIFICATION_RESULT,
                EventType.REPORTER_RESPONSE,
                EventType.REPORTER_COMMENT,
                EventType.RETRY_REQUESTED,
            ),
            _s(
                IssueState.TRIAGE,
                IssueState.AWAITING_REPORTER,
                IssueState.BLOCKED_ENVIRONMENT,
                IssueState.AUTOMATION_ERROR,
            ),
            _s(IssueState.REPRODUCING),
            _a(ActorRole.SYSTEM, ActorRole.AGENT, ActorRole.OPERATOR, ActorRole.REPORTER),
            preconditions=("all required questions answered", "no unavailable evidence"),
            description="Create a bounded reproduction session against the recorded commit.",
        ),
        TransitionSpec(
            TransitionName.REPRODUCTION_COMPLETED,
            _e(EventType.REPRODUCTION_RESULT),
            _s(IssueState.REPRODUCING),
            _s(IssueState.NEEDS_OWNER_DECISION),
            _a(ActorRole.AGENT),
            preconditions=("result revision matches current revision",),
            description="Release the workspace and hand an evidence packet to the owner.",
        ),
        TransitionSpec(
            TransitionName.ENVIRONMENT_BLOCKED,
            _e(EventType.ENVIRONMENT_BLOCKED),
            _s(IssueState.REPRODUCING),
            _s(IssueState.BLOCKED_ENVIRONMENT),
            _a(ActorRole.AGENT, ActorRole.SYSTEM),
        ),
        TransitionSpec(
            TransitionName.CONFIRM_BUG,
            _e(EventType.OWNER_DECISION),
            _s(IssueState.NEEDS_OWNER_DECISION),
            _s(IssueState.FIX_AUTHORIZED),
            _a(ActorRole.OWNER),
            human_gate=True,
            preconditions=("decision is confirm_bug",),
            description="Owner confirms expected behavior and authorizes a scoped fix.",
        ),
        TransitionSpec(
            TransitionName.REQUEST_DISCRIMINATOR,
            _e(EventType.OWNER_DECISION),
            _s(IssueState.NEEDS_OWNER_DECISION),
            _s(IssueState.AWAITING_REPORTER),
            _a(ActorRole.OWNER),
            human_gate=True,
            public_side_effects=("issue_comment",),
        ),
        TransitionSpec(
            TransitionName.RECLASSIFY,
            _e(EventType.OWNER_DECISION),
            _s(IssueState.TRIAGE, IssueState.NEEDS_OWNER_DECISION, IssueState.BLOCKED_ENVIRONMENT),
            _s(IssueState.NOT_A_BUG, IssueState.UNSUPPORTED, IssueState.DUPLICATE),
            _a(ActorRole.OWNER),
            human_gate=True,
        ),
        TransitionSpec(
            TransitionName.ROUTE_SECURITY_PRIVATE,
            _e(
                EventType.SECURITY_SIGNAL, EventType.CLASSIFICATION_RESULT, EventType.OWNER_DECISION
            ),
            _NON_TERMINAL,
            _s(IssueState.SECURITY_PRIVATE),
            _a(ActorRole.SYSTEM, ActorRole.OWNER, ActorRole.SECURITY, ActorRole.OPERATOR),
            description="Fail closed: stop public processing and create a private task.",
        ),
        TransitionSpec(
            TransitionName.START_FIX,
            _e(EventType.FIX_SESSION_STARTED),
            _s(IssueState.FIX_AUTHORIZED),
            _s(IssueState.FIXING),
            _a(ActorRole.SYSTEM, ActorRole.OPERATOR),
            preconditions=("unexpired confirm_bug authorization exists",),
        ),
        TransitionSpec(
            TransitionName.OPEN_PR,
            _e(EventType.FIX_RESULT),
            _s(IssueState.FIXING),
            _s(IssueState.PR_OPEN),
            _a(ActorRole.AGENT),
            preconditions=("pull request targets exloong/superset",),
            public_side_effects=("pull_request", "review_request"),
        ),
        TransitionSpec(
            TransitionName.PR_HEAD_UPDATED,
            _e(EventType.PR_SYNCHRONIZED),
            _s(IssueState.PR_OPEN, IssueState.AWAITING_OWNER, IssueState.CHANGES_REQUESTED),
            _s(IssueState.PR_OPEN),
            _a(ActorRole.SYSTEM),
            preconditions=("PR number is tracked for this issue",),
            description=(
                "New head commit invalidates prior human approval and Devin Review evidence; "
                "review and reviewer routing run again for the new head."
            ),
            public_side_effects=("review_request",),
        ),
        TransitionSpec(
            TransitionName.DEVIN_REVIEW_RECORDED,
            _e(EventType.DEVIN_REVIEW_COMPLETED),
            _s(IssueState.PR_OPEN, IssueState.AWAITING_OWNER),
            _s(IssueState.AWAITING_OWNER),
            _a(ActorRole.AGENT, ActorRole.SYSTEM),
            description="Attach Devin Review evidence; never counts as approval.",
        ),
        TransitionSpec(
            TransitionName.OWNER_REQUESTED_CHANGES,
            _e(EventType.OWNER_DECISION, EventType.HUMAN_REVIEW_SUBMITTED),
            _s(IssueState.PR_OPEN, IssueState.AWAITING_OWNER),
            _s(IssueState.CHANGES_REQUESTED),
            _a(ActorRole.OWNER, ActorRole.OPERATOR),
            human_gate=True,
            preconditions=(
                "reviewer is in the persisted CODEOWNERS routing for the PR head, "
                "or an operator records an explicit override",
            ),
        ),
        TransitionSpec(
            TransitionName.RESUME_FIX,
            _e(EventType.FIX_SESSION_STARTED),
            _s(IssueState.CHANGES_REQUESTED),
            _s(IssueState.FIXING),
            _a(ActorRole.SYSTEM, ActorRole.OPERATOR),
        ),
        TransitionSpec(
            TransitionName.OWNER_APPROVED,
            _e(EventType.OWNER_DECISION, EventType.HUMAN_REVIEW_SUBMITTED),
            _s(IssueState.PR_OPEN, IssueState.AWAITING_OWNER),
            _s(IssueState.AWAITING_OWNER),
            _a(ActorRole.OWNER, ActorRole.OPERATOR),
            human_gate=True,
            preconditions=(
                "approval names the exact PR number and head SHA",
                "reviewer is in the persisted CODEOWNERS routing for that head, "
                "or an operator records an explicit override",
            ),
            description="Record human approval. Merge remains a human GitHub action.",
        ),
        TransitionSpec(
            TransitionName.COMPLETE,
            _e(EventType.PR_MERGED),
            _s(IssueState.AWAITING_OWNER, IssueState.PR_OPEN),
            _s(IssueState.COMPLETED),
            _a(ActorRole.SYSTEM),
            preconditions=("a human approve_pr decision exists for the merged PR head SHA",),
        ),
        TransitionSpec(
            TransitionName.REMIND_REPORTER,
            _e(EventType.REMINDER_ELAPSED),
            _s(IssueState.AWAITING_REPORTER),
            _s(IssueState.AWAITING_REPORTER),
            _a(ActorRole.SYSTEM),
            preconditions=("timer due_at and policy_revision match the current wait policy",),
            public_side_effects=("issue_comment",),
        ),
        TransitionSpec(
            TransitionName.CLOSE_INACTIVE,
            _e(EventType.INACTIVITY_ELAPSED),
            _s(IssueState.AWAITING_REPORTER),
            _s(IssueState.CLOSED_INACTIVE),
            _a(ActorRole.SYSTEM),
            preconditions=("timer due_at and policy_revision match the current wait policy",),
        ),
        TransitionSpec(
            TransitionName.MARK_DUPLICATE,
            _e(EventType.DUPLICATE_DETECTED, EventType.CLASSIFICATION_RESULT),
            _s(IssueState.TRIAGE),
            _s(IssueState.DUPLICATE),
            _a(ActorRole.SYSTEM, ActorRole.AGENT),
        ),
        TransitionSpec(
            TransitionName.REOPEN,
            _e(EventType.ISSUE_REOPENED),
            TERMINAL_STATES,
            _s(IssueState.TRIAGE),
            _a(ActorRole.SYSTEM, ActorRole.REPORTER, ActorRole.OWNER),
            description="Create a new issue revision; history is preserved.",
        ),
        TransitionSpec(
            TransitionName.AUTOMATION_FAILED,
            _e(EventType.AUTOMATION_FAILURE),
            _RECOVERABLE,
            _s(IssueState.AUTOMATION_ERROR),
            _a(ActorRole.SYSTEM, ActorRole.AGENT),
        ),
        TransitionSpec(
            TransitionName.RETRY,
            _e(EventType.RETRY_REQUESTED),
            _s(IssueState.AUTOMATION_ERROR, IssueState.BLOCKED_ENVIRONMENT),
            _RECOVERABLE - ACTIVE_AGENT_STATES,
            _a(ActorRole.OPERATOR),
            human_gate=True,
            description=(
                "Return to the safe gate recorded before the failure; agent-active states "
                "are re-entered only by starting a new bounded session."
            ),
        ),
    )
}


def transitions_for_event(event: EventType) -> list[TransitionSpec]:
    return [spec for spec in TRANSITIONS.values() if event in spec.events]
