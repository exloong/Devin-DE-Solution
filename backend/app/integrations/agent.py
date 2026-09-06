"""Typed, bounded contracts for safe mock agent work."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Protocol, TypeAlias
from uuid import UUID, uuid5

from .errors import ContractValidationError, ValidationCode

TASK_ENVELOPE_SCHEMA = "agent_task.v1"


class AgentTaskKind(str, Enum):
    CLASSIFICATION = "classification"
    REPRODUCTION = "reproduction"
    EVIDENCE_PACKET = "evidence_packet"
    SCOPED_FIX = "scoped_fix"


class AgentCapability(str, Enum):
    READ_ISSUE_CONTEXT = "read_issue_context"
    READ_REPOSITORY = "read_repository"
    RUN_ISOLATED_TESTS = "run_isolated_tests"
    WRITE_SCOPED_PATCH = "write_scoped_patch"


_OUTPUT_SCHEMAS: Mapping[AgentTaskKind, str] = MappingProxyType(
    {
        AgentTaskKind.CLASSIFICATION: "classification_result.v1",
        AgentTaskKind.REPRODUCTION: "reproduction_result.v1",
        AgentTaskKind.EVIDENCE_PACKET: "evidence_packet_result.v1",
        AgentTaskKind.SCOPED_FIX: "scoped_fix_result.v1",
    }
)

_DEFAULT_CAPABILITIES: Mapping[AgentTaskKind, frozenset[AgentCapability]] = (
    MappingProxyType(
        {
            AgentTaskKind.CLASSIFICATION: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_REPOSITORY,
                }
            ),
            AgentTaskKind.REPRODUCTION: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_REPOSITORY,
                    AgentCapability.RUN_ISOLATED_TESTS,
                }
            ),
            AgentTaskKind.EVIDENCE_PACKET: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_REPOSITORY,
                }
            ),
            AgentTaskKind.SCOPED_FIX: frozenset(
                {
                    AgentCapability.READ_ISSUE_CONTEXT,
                    AgentCapability.READ_REPOSITORY,
                    AgentCapability.RUN_ISOLATED_TESTS,
                    AgentCapability.WRITE_SCOPED_PATCH,
                }
            ),
        }
    )
)


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _require_non_nil_uuid(value: UUID, field_name: str) -> None:
    if not isinstance(value, UUID) or value.int == 0:
        raise ValueError(f"{field_name} cannot be the nil UUID")


@dataclass(frozen=True, slots=True)
class AgentBudget:
    wall_seconds: int
    retry_limit: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        if self.wall_seconds < 1:
            raise ValueError("wall_seconds must be positive")
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        if self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")


@dataclass(frozen=True, slots=True)
class AgentPolicy:
    max_wall_seconds: int = 3_600
    max_retry_limit: int = 2
    max_output_bytes: int = 1_048_576
    allowed_capabilities: Mapping[
        AgentTaskKind, frozenset[AgentCapability]
    ] = _DEFAULT_CAPABILITIES


@dataclass(frozen=True, slots=True)
class AgentTaskEnvelope:
    task_id: UUID
    issue_id: UUID
    issue_revision: int
    kind: AgentTaskKind
    created_at: datetime
    budget: AgentBudget
    allowed_capabilities: frozenset[AgentCapability]
    input_artifact_ids: tuple[UUID, ...]
    output_schema: str
    envelope_schema: str = TASK_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        _require_non_nil_uuid(self.task_id, "task_id")
        _require_non_nil_uuid(self.issue_id, "issue_id")
        if self.issue_revision < 1:
            raise ValueError("issue_revision must be positive")
        _require_utc(self.created_at, "created_at")
        for artifact_id in self.input_artifact_ids:
            _require_non_nil_uuid(artifact_id, "input_artifact_id")
        if len(set(self.input_artifact_ids)) != len(self.input_artifact_ids):
            raise ValueError("input_artifact_ids must be unique")

    @property
    def deadline(self) -> datetime:
        return self.created_at + timedelta(seconds=self.budget.wall_seconds)


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    classification: str
    confidence: float
    evidence_artifact_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.classification not in {
            "bug",
            "not_a_bug",
            "needs_information",
            "security_private",
        }:
            raise ValueError("classification is unsupported")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ReproductionResult:
    reproduced: bool
    attempts: int
    observed_behavior: str
    target_behavior: str
    control_behavior: str
    artifact_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be positive")


@dataclass(frozen=True, slots=True)
class EvidencePacketResult:
    observed_behavior: str
    expected_behavior_evidence: str
    environment: str
    repeat_count: int
    remaining_uncertainty: str
    artifact_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if self.repeat_count < 1:
            raise ValueError("repeat_count must be positive")


@dataclass(frozen=True, slots=True)
class ScopedFixResult:
    summary: str
    patch_artifact_id: UUID
    regression_test_artifact_ids: tuple[UUID, ...]


AgentResultPayload: TypeAlias = (
    ClassificationResult
    | ReproductionResult
    | EvidencePacketResult
    | ScopedFixResult
)


@dataclass(frozen=True, slots=True)
class AgentResultEnvelope:
    task_id: UUID
    issue_id: UUID
    issue_revision: int
    kind: AgentTaskKind
    completed_at: datetime
    output_schema: str
    output_size_bytes: int
    payload: AgentResultPayload

    def __post_init__(self) -> None:
        _require_non_nil_uuid(self.task_id, "task_id")
        _require_non_nil_uuid(self.issue_id, "issue_id")
        if self.issue_revision < 1:
            raise ValueError("issue_revision must be positive")
        _require_utc(self.completed_at, "completed_at")
        if self.output_size_bytes < 0:
            raise ValueError("output_size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class ResultAcceptance:
    task_id: UUID
    issue_id: UUID
    issue_revision: int
    accepted_at: datetime


_PAYLOAD_TYPES: Mapping[AgentTaskKind, type[AgentResultPayload]] = MappingProxyType(
    {
        AgentTaskKind.CLASSIFICATION: ClassificationResult,
        AgentTaskKind.REPRODUCTION: ReproductionResult,
        AgentTaskKind.EVIDENCE_PACKET: EvidencePacketResult,
        AgentTaskKind.SCOPED_FIX: ScopedFixResult,
    }
)


def validate_agent_task(
    task: AgentTaskEnvelope,
    policy: AgentPolicy = AgentPolicy(),
) -> None:
    if not isinstance(task.kind, AgentTaskKind):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            "agent task kind is unsupported",
        )
    if task.envelope_schema != TASK_ENVELOPE_SCHEMA:
        raise ContractValidationError(
            ValidationCode.OUTPUT_SCHEMA_MISMATCH,
            "agent task envelope schema is unsupported",
        )
    if task.output_schema != _OUTPUT_SCHEMAS.get(task.kind):
        raise ContractValidationError(
            ValidationCode.OUTPUT_SCHEMA_MISMATCH,
            "agent task output schema does not match its kind",
        )
    if (
        task.budget.wall_seconds > policy.max_wall_seconds
        or task.budget.retry_limit > policy.max_retry_limit
        or task.budget.max_output_bytes > policy.max_output_bytes
    ):
        raise ContractValidationError(
            ValidationCode.INVALID_BUDGET,
            "agent task exceeds its budget policy",
        )

    allowed_for_kind = policy.allowed_capabilities.get(task.kind, frozenset())
    if any(
        not isinstance(capability, AgentCapability)
        for capability in task.allowed_capabilities
    ) or not task.allowed_capabilities.issubset(allowed_for_kind):
        raise ContractValidationError(
            ValidationCode.PROHIBITED_CAPABILITY,
            "agent task requests a prohibited capability",
        )


def validate_result(
    *,
    task: AgentTaskEnvelope,
    result: AgentResultEnvelope,
    current_issue_revision: int,
    received_at: datetime,
) -> ResultAcceptance:
    _require_utc(received_at, "received_at")
    validate_agent_task(task)

    if not isinstance(result.kind, AgentTaskKind):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            "agent result kind is unsupported",
        )
    if (
        result.task_id != task.task_id
        or result.issue_id != task.issue_id
        or result.kind != task.kind
    ):
        raise ContractValidationError(
            ValidationCode.TASK_IDENTITY_MISMATCH,
            "agent result does not match its task identity",
        )
    if result.issue_revision != task.issue_revision:
        raise ContractValidationError(
            ValidationCode.ISSUE_REVISION_MISMATCH,
            "agent result revision does not match its task",
        )
    if current_issue_revision != task.issue_revision:
        raise ContractValidationError(
            ValidationCode.STALE_RESULT,
            "agent result targets a stale issue revision",
        )
    if result.completed_at > task.deadline or received_at > task.deadline:
        raise ContractValidationError(
            ValidationCode.LATE_RESULT,
            "agent result arrived after its task deadline",
        )
    if result.output_schema != task.output_schema:
        raise ContractValidationError(
            ValidationCode.OUTPUT_SCHEMA_MISMATCH,
            "agent result output schema does not match its task",
        )
    expected_payload_type = _PAYLOAD_TYPES[task.kind]
    if not isinstance(result.payload, expected_payload_type):
        raise ContractValidationError(
            ValidationCode.OUTPUT_PAYLOAD_MISMATCH,
            "agent result payload does not match its output schema",
        )
    if result.output_size_bytes > task.budget.max_output_bytes:
        raise ContractValidationError(
            ValidationCode.INVALID_BUDGET,
            "agent result exceeds its output budget",
        )
    return ResultAcceptance(
        task_id=task.task_id,
        issue_id=task.issue_id,
        issue_revision=task.issue_revision,
        accepted_at=received_at,
    )


class AgentAdapter(Protocol):
    def run(self, task: AgentTaskEnvelope) -> AgentResultEnvelope:
        """Run one bounded task and return its typed result envelope."""


class DeterministicMockAgentAdapter:
    def __init__(self, policy: AgentPolicy = AgentPolicy()) -> None:
        self._policy = policy

    def run(self, task: AgentTaskEnvelope) -> AgentResultEnvelope:
        validate_agent_task(task, self._policy)
        artifact_id = uuid5(task.task_id, task.kind.value)
        payload: AgentResultPayload
        if task.kind is AgentTaskKind.CLASSIFICATION:
            payload = ClassificationResult(
                classification="bug",
                confidence=1.0,
                evidence_artifact_ids=task.input_artifact_ids,
            )
        elif task.kind is AgentTaskKind.REPRODUCTION:
            payload = ReproductionResult(
                reproduced=True,
                attempts=1,
                observed_behavior="Deterministic mock observed the configured failure.",
                target_behavior="Target behavior differs from the control.",
                control_behavior="Control behavior remains stable.",
                artifact_ids=(artifact_id,),
            )
        elif task.kind is AgentTaskKind.EVIDENCE_PACKET:
            payload = EvidencePacketResult(
                observed_behavior="Deterministic mock observed the configured failure.",
                expected_behavior_evidence="Local fixture defines the expected behavior.",
                environment="deterministic-mock",
                repeat_count=1,
                remaining_uncertainty="Owner confirmation is still required.",
                artifact_ids=(artifact_id,),
            )
        else:
            payload = ScopedFixResult(
                summary="Prepared a deterministic scoped patch artifact.",
                patch_artifact_id=artifact_id,
                regression_test_artifact_ids=(uuid5(task.task_id, "regression-test"),),
            )
        return AgentResultEnvelope(
            task_id=task.task_id,
            issue_id=task.issue_id,
            issue_revision=task.issue_revision,
            kind=task.kind,
            completed_at=task.created_at,
            output_schema=task.output_schema,
            output_size_bytes=len(repr(payload).encode("utf-8")),
            payload=payload,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
