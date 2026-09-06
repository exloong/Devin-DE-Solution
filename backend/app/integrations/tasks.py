"""Typed task and result envelopes for bounded Superset agent sessions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from uuid import UUID

from .errors import ContractValidationError, ValidationCode
from .repository import (
    SUPERSET_REPOSITORY,
    RepositoryIdentity,
    TargetCommit,
    require_superset_repository,
    validate_pull_request_url,
    validate_superset_repository_field,
)

TASK_ENVELOPE_SCHEMA = "relay_agent_task.v1"


class TaskKind(str, Enum):
    """The four bounded session kinds Relay may create."""

    CLASSIFICATION = "classification"
    REPRODUCTION = "reproduction"
    EVIDENCE_PACKET = "evidence_packet"
    FIX = "fix"


class AgentCapability(str, Enum):
    """A capability a bounded session may be granted."""

    READ_ISSUE_CONTEXT = "read_issue_context"
    READ_SUPERSET_REPOSITORY = "read_superset_repository"
    RUN_ISOLATED_TESTS = "run_isolated_tests"
    WRITE_SCOPED_PATCH = "write_scoped_patch"
    OPEN_SUPERSET_PULL_REQUEST = "open_superset_pull_request"


PROHIBITED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "merge_pull_request",
        "close_issue_as_fixed",
        "publish_security_report",
        "execute_reporter_content",
        "write_other_repository",
        "read_production_secrets",
    }
)

OUTPUT_SCHEMAS: Mapping[TaskKind, str] = MappingProxyType(
    {
        TaskKind.CLASSIFICATION: "classification_result.v1",
        TaskKind.REPRODUCTION: "reproduction_result.v1",
        TaskKind.EVIDENCE_PACKET: "evidence_packet_result.v1",
        TaskKind.FIX: "fix_result.v1",
    }
)

DEFAULT_ALLOWED_CAPABILITIES: Mapping[TaskKind, frozenset[AgentCapability]] = (
    MappingProxyType(
        {
            TaskKind.CLASSIFICATION: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_SUPERSET_REPOSITORY,
                }
            ),
            TaskKind.REPRODUCTION: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_SUPERSET_REPOSITORY,
                    AgentCapability.RUN_ISOLATED_TESTS,
                }
            ),
            TaskKind.EVIDENCE_PACKET: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_SUPERSET_REPOSITORY,
                }
            ),
            TaskKind.FIX: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_SUPERSET_REPOSITORY,
                    AgentCapability.RUN_ISOLATED_TESTS,
                    AgentCapability.WRITE_SCOPED_PATCH,
                    AgentCapability.OPEN_SUPERSET_PULL_REQUEST,
                }
            ),
        }
    )
)


class WorkspaceStatus(str, Enum):
    """Whether a session still holds a remote workspace."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    RELEASED = "released"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: object, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} must be a timezone-aware UTC datetime",
        )
    return value


def _require_uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} must be a non-nil UUID",
        )
    return value


@dataclass(frozen=True)
class CapabilityBudget:
    """Wall-clock, retry, and output limits enforced for one session."""

    wall_seconds: int
    retry_limit: int = 1
    max_output_bytes: int = 262_144

    def __post_init__(self) -> None:
        if self.wall_seconds < 1:
            raise ContractValidationError(
                ValidationCode.INVALID_BUDGET, "wall_seconds must be positive"
            )
        if self.retry_limit < 0:
            raise ContractValidationError(
                ValidationCode.INVALID_BUDGET, "retry_limit cannot be negative"
            )
        if self.max_output_bytes < 1:
            raise ContractValidationError(
                ValidationCode.INVALID_BUDGET, "max_output_bytes must be positive"
            )


@dataclass(frozen=True)
class TaskPolicy:
    """Ceilings applied to every task envelope before a session is created."""

    max_wall_seconds: int = 3_600
    max_retry_limit: int = 2
    max_output_bytes: int = 1_048_576
    allowed_capabilities: Mapping[TaskKind, frozenset[AgentCapability]] = (
        DEFAULT_ALLOWED_CAPABILITIES
    )


DEFAULT_TASK_POLICY = TaskPolicy()


@dataclass(frozen=True)
class TaskEnvelope:
    """One bounded unit of agent work against an immutable Superset commit."""

    task_id: UUID
    issue_id: UUID
    issue_revision: int
    kind: TaskKind
    created_at: datetime
    target_commit: TargetCommit
    budget: CapabilityBudget
    allowed_capabilities: frozenset[AgentCapability]
    objective: str
    input_artifact_ids: tuple[UUID, ...] = ()
    repository: RepositoryIdentity = SUPERSET_REPOSITORY
    envelope_schema: str = TASK_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        _require_uuid(self.task_id, "task_id")
        _require_uuid(self.issue_id, "issue_id")
        _require_utc(self.created_at, "created_at")
        if not isinstance(self.kind, TaskKind):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "task kind is unsupported"
            )
        if not isinstance(self.issue_revision, int) or self.issue_revision < 1:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "issue_revision must be positive"
            )
        if not isinstance(self.target_commit, TargetCommit):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "target_commit must be a TargetCommit",
            )
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "objective must be non-empty text"
            )
        for artifact_id in self.input_artifact_ids:
            _require_uuid(artifact_id, "input_artifact_id")
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "input_artifact_ids must be unique",
            )
        if not isinstance(self.allowed_capabilities, frozenset):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "allowed_capabilities must be a frozenset",
            )
        validate_superset_repository_field(self.repository)

    @property
    def output_schema(self) -> str:
        """The result schema this task kind must return."""
        return OUTPUT_SCHEMAS[self.kind]

    @property
    def deadline(self) -> datetime:
        return self.created_at + timedelta(seconds=self.budget.wall_seconds)


@dataclass(frozen=True)
class ClassificationOutput:
    classification: str
    confidence: float
    rationale: str
    evidence_artifact_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.classification not in {
            "bug",
            "not_a_bug",
            "needs_information",
            "suspected_security",
            "duplicate",
            "unsupported",
        }:
            raise ContractValidationError(
                ValidationCode.OUTPUT_PAYLOAD_MISMATCH,
                "classification value is unsupported",
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ContractValidationError(
                ValidationCode.OUTPUT_PAYLOAD_MISMATCH,
                "confidence must be between 0 and 1",
            )


@dataclass(frozen=True)
class ReproductionOutput:
    reproduced: bool
    attempts: int
    observed_behavior: str
    target_behavior: str
    control_behavior: str
    artifact_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ContractValidationError(
                ValidationCode.OUTPUT_PAYLOAD_MISMATCH, "attempts must be positive"
            )


@dataclass(frozen=True)
class EvidencePacketOutput:
    observed_behavior: str
    expected_behavior_evidence: str
    environment: str
    minimal_condition: str
    repeat_count: int
    remaining_uncertainty: str
    security_classification: str = "not_security_sensitive"
    artifact_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if self.repeat_count < 1:
            raise ContractValidationError(
                ValidationCode.OUTPUT_PAYLOAD_MISMATCH, "repeat_count must be positive"
            )


@dataclass(frozen=True)
class PullRequestLink:
    """A Superset pull request produced by a fix session."""

    repository: RepositoryIdentity
    number: int
    html_url: str
    head_branch: str | None = None
    state: str | None = None

    def __post_init__(self) -> None:
        validate_superset_repository_field(self.repository)
        if isinstance(self.number, bool) or (
            not isinstance(self.number, int) or self.number < 1
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "pull request number must be positive",
            )
        validate_pull_request_url(self.html_url, pull_request_number=self.number)
        for name, value in (("head_branch", self.head_branch), ("state", self.state)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ContractValidationError(
                    ValidationCode.MALFORMED_RESPONSE,
                    f"pull request {name} must be non-empty text when present",
                )


@dataclass(frozen=True)
class FixOutput:
    summary: str
    branch_name: str
    pull_request: PullRequestLink | None
    regression_test_paths: tuple[str, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ContractValidationError(
                ValidationCode.OUTPUT_PAYLOAD_MISMATCH, "summary must be non-empty"
            )


ResultPayload = (
    ClassificationOutput | ReproductionOutput | EvidencePacketOutput | FixOutput
)

PAYLOAD_TYPES: Mapping[TaskKind, type] = MappingProxyType(
    {
        TaskKind.CLASSIFICATION: ClassificationOutput,
        TaskKind.REPRODUCTION: ReproductionOutput,
        TaskKind.EVIDENCE_PACKET: EvidencePacketOutput,
        TaskKind.FIX: FixOutput,
    }
)


@dataclass(frozen=True)
class ResultEnvelope:
    """A typed result returned by one bounded session."""

    task_id: UUID
    issue_id: UUID
    issue_revision: int
    kind: TaskKind
    session_id: str
    completed_at: datetime
    target_commit: TargetCommit
    workspace_status: WorkspaceStatus
    output_schema: str
    output_size_bytes: int
    payload: ResultPayload
    repository: RepositoryIdentity = SUPERSET_REPOSITORY

    def __post_init__(self) -> None:
        _require_uuid(self.task_id, "task_id")
        _require_uuid(self.issue_id, "issue_id")
        _require_utc(self.completed_at, "completed_at")
        if not isinstance(self.kind, TaskKind):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "result kind is unsupported"
            )
        if not isinstance(self.workspace_status, WorkspaceStatus):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "workspace status is unsupported"
            )
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "session_id must be non-empty text"
            )
        if self.output_size_bytes < 0:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "output_size_bytes cannot be negative",
            )
        validate_superset_repository_field(self.repository)


@dataclass(frozen=True)
class ResultAcceptance:
    """Proof that a result may advance the lifecycle."""

    task_id: UUID
    issue_id: UUID
    issue_revision: int
    kind: TaskKind
    accepted_at: datetime
    workspace_released: bool


def validate_task(task: TaskEnvelope, policy: TaskPolicy | None = None) -> None:
    """Reject a task whose schema, budget, or capabilities exceed policy."""
    policy = policy or DEFAULT_TASK_POLICY
    if task.envelope_schema != TASK_ENVELOPE_SCHEMA:
        raise ContractValidationError(
            ValidationCode.OUTPUT_SCHEMA_MISMATCH,
            "task envelope schema is unsupported",
        )
    require_superset_repository(task.repository)
    if (
        task.budget.wall_seconds > policy.max_wall_seconds
        or task.budget.retry_limit > policy.max_retry_limit
        or task.budget.max_output_bytes > policy.max_output_bytes
    ):
        raise ContractValidationError(
            ValidationCode.INVALID_BUDGET, "task exceeds its budget policy"
        )
    if any(
        not isinstance(capability, AgentCapability)
        or capability.value in PROHIBITED_CAPABILITIES
        for capability in task.allowed_capabilities
    ):
        raise ContractValidationError(
            ValidationCode.PROHIBITED_CAPABILITY,
            "task requests a capability Relay may never grant",
        )
    allowed_for_kind = policy.allowed_capabilities.get(task.kind, frozenset())
    if not task.allowed_capabilities.issubset(allowed_for_kind):
        raise ContractValidationError(
            ValidationCode.PROHIBITED_CAPABILITY,
            f"task requests a capability outside the {task.kind.value} allowlist",
        )


def validate_result(
    *,
    task: TaskEnvelope,
    result: ResultEnvelope,
    current_issue_revision: int,
    received_at: datetime,
    policy: TaskPolicy | None = None,
) -> ResultAcceptance:
    """Accept a result only when it matches its task, revision, and commit."""
    policy = policy or DEFAULT_TASK_POLICY
    _require_utc(received_at, "received_at")
    validate_task(task, policy)

    if (
        result.task_id != task.task_id
        or result.issue_id != task.issue_id
        or result.kind != task.kind
    ):
        raise ContractValidationError(
            ValidationCode.TASK_IDENTITY_MISMATCH,
            "result does not match its task identity",
        )
    if result.repository != task.repository:
        raise ContractValidationError(
            ValidationCode.UNAUTHORIZED_REPOSITORY,
            "result targets a repository other than the task repository",
        )
    if result.target_commit != task.target_commit:
        raise ContractValidationError(
            ValidationCode.TARGET_COMMIT_MISMATCH,
            "result targets a commit other than the immutable task commit",
        )
    if result.issue_revision != task.issue_revision:
        raise ContractValidationError(
            ValidationCode.ISSUE_REVISION_MISMATCH,
            "result revision does not match its task revision",
        )
    if current_issue_revision != task.issue_revision:
        raise ContractValidationError(
            ValidationCode.STALE_RESULT,
            "result targets a superseded issue revision",
        )
    if result.completed_at > task.deadline or received_at > task.deadline:
        raise ContractValidationError(
            ValidationCode.LATE_RESULT, "result arrived after its task deadline"
        )
    if result.output_schema != task.output_schema:
        raise ContractValidationError(
            ValidationCode.OUTPUT_SCHEMA_MISMATCH,
            "result output schema does not match its task",
        )
    if not isinstance(result.payload, PAYLOAD_TYPES[task.kind]):
        raise ContractValidationError(
            ValidationCode.OUTPUT_PAYLOAD_MISMATCH,
            "result payload does not match its output schema",
        )
    if result.output_size_bytes > task.budget.max_output_bytes:
        raise ContractValidationError(
            ValidationCode.INVALID_BUDGET, "result exceeds its output budget"
        )
    if (
        isinstance(result.payload, FixOutput)
        and result.payload.pull_request is not None
    ):
        require_superset_repository(result.payload.pull_request.repository)
    if result.workspace_status is WorkspaceStatus.ACTIVE:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "a completed result may not report an active workspace",
        )
    return ResultAcceptance(
        task_id=task.task_id,
        issue_id=task.issue_id,
        issue_revision=task.issue_revision,
        kind=task.kind,
        accepted_at=received_at,
        workspace_released=result.workspace_status is WorkspaceStatus.RELEASED,
    )
