from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from backend.app.integrations.agent import (
    AgentBudget,
    AgentCapability,
    AgentPolicy,
    AgentResultEnvelope,
    AgentTaskEnvelope,
    AgentTaskKind,
    ClassificationResult,
    DeterministicMockAgentAdapter,
    ReproductionResult,
    validate_agent_task,
    validate_result,
)
from backend.app.integrations.errors import ContractValidationError, ValidationCode

_OUTPUT_SCHEMAS = {
    AgentTaskKind.CLASSIFICATION: "classification_result.v1",
    AgentTaskKind.REPRODUCTION: "reproduction_result.v1",
    AgentTaskKind.EVIDENCE_PACKET: "evidence_packet_result.v1",
    AgentTaskKind.SCOPED_FIX: "scoped_fix_result.v1",
}

_CAPABILITIES = {
    AgentTaskKind.CLASSIFICATION: frozenset(
        {AgentCapability.READ_ISSUE_CONTEXT, AgentCapability.READ_REPOSITORY}
    ),
    AgentTaskKind.REPRODUCTION: frozenset(
        {
            AgentCapability.READ_ISSUE_CONTEXT,
            AgentCapability.READ_REPOSITORY,
            AgentCapability.RUN_ISOLATED_TESTS,
        }
    ),
    AgentTaskKind.EVIDENCE_PACKET: frozenset(
        {AgentCapability.READ_ISSUE_CONTEXT, AgentCapability.READ_REPOSITORY}
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


class AgentContractTestCase(unittest.TestCase):
    created_at = datetime(2026, 9, 6, tzinfo=timezone.utc)

    def task(self, kind: AgentTaskKind = AgentTaskKind.CLASSIFICATION) -> AgentTaskEnvelope:
        return AgentTaskEnvelope(
            task_id=uuid4(),
            issue_id=uuid4(),
            issue_revision=7,
            kind=kind,
            created_at=self.created_at,
            budget=AgentBudget(
                wall_seconds=600,
                retry_limit=1,
                max_output_bytes=64_000,
            ),
            allowed_capabilities=_CAPABILITIES[kind],
            input_artifact_ids=(uuid4(),),
            output_schema=_OUTPUT_SCHEMAS[kind],
        )


class AgentTaskValidationTests(AgentContractTestCase):
    def test_accepts_bounded_capabilities_for_each_task_kind(self) -> None:
        for kind in AgentTaskKind:
            with self.subTest(kind=kind):
                validate_agent_task(self.task(kind))

    def test_rejects_prohibited_capability_not_present_in_typed_enum(self) -> None:
        task = replace(
            self.task(),
            allowed_capabilities=frozenset({"arbitrary_shell"}),
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_agent_task(task)

        self.assertEqual(raised.exception.code, ValidationCode.PROHIBITED_CAPABILITY)

    def test_rejects_capability_outside_task_specific_allowlist(self) -> None:
        task = replace(
            self.task(AgentTaskKind.CLASSIFICATION),
            allowed_capabilities=frozenset({AgentCapability.WRITE_SCOPED_PATCH}),
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_agent_task(task)

        self.assertEqual(raised.exception.code, ValidationCode.PROHIBITED_CAPABILITY)

    def test_rejects_budget_over_policy(self) -> None:
        task = replace(
            self.task(),
            budget=AgentBudget(
                wall_seconds=3_601,
                retry_limit=1,
                max_output_bytes=64_000,
            ),
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_agent_task(task, AgentPolicy())

        self.assertEqual(raised.exception.code, ValidationCode.INVALID_BUDGET)

    def test_rejects_wrong_output_schema_version(self) -> None:
        task = replace(self.task(), output_schema="classification_result.v2")

        with self.assertRaises(ContractValidationError) as raised:
            validate_agent_task(task)

        self.assertEqual(raised.exception.code, ValidationCode.OUTPUT_SCHEMA_MISMATCH)

    def test_rejects_untyped_task_kind(self) -> None:
        task = replace(self.task(), kind="classification")

        with self.assertRaises(ContractValidationError) as raised:
            validate_agent_task(task)

        self.assertEqual(raised.exception.code, ValidationCode.MALFORMED_ENVELOPE)

    def test_rejects_nil_task_identity(self) -> None:
        with self.assertRaises(ValueError):
            replace(self.task(), task_id=UUID(int=0))


class AgentResultValidationTests(AgentContractTestCase):
    def valid_result(
        self,
        task: AgentTaskEnvelope,
    ) -> AgentResultEnvelope:
        return DeterministicMockAgentAdapter().run(task)

    def test_accepts_matching_current_result(self) -> None:
        task = self.task()
        result = self.valid_result(task)
        received_at = task.created_at + timedelta(seconds=1)

        acceptance = validate_result(
            task=task,
            result=result,
            current_issue_revision=task.issue_revision,
            received_at=received_at,
        )

        self.assertEqual(acceptance.task_id, task.task_id)
        self.assertEqual(acceptance.accepted_at, received_at)

    def test_rejects_stale_issue_revision(self) -> None:
        task = self.task()

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=self.valid_result(task),
                current_issue_revision=task.issue_revision + 1,
                received_at=task.created_at + timedelta(seconds=1),
            )

        self.assertEqual(raised.exception.code, ValidationCode.STALE_RESULT)

    def test_rejects_late_result(self) -> None:
        task = self.task()

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=self.valid_result(task),
                current_issue_revision=task.issue_revision,
                received_at=task.deadline + timedelta(microseconds=1),
            )

        self.assertEqual(raised.exception.code, ValidationCode.LATE_RESULT)

    def test_rejects_mismatched_task_identity(self) -> None:
        task = self.task()
        result = replace(self.valid_result(task), task_id=uuid4())

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=result,
                current_issue_revision=task.issue_revision,
                received_at=task.created_at,
            )

        self.assertEqual(
            raised.exception.code,
            ValidationCode.TASK_IDENTITY_MISMATCH,
        )

    def test_rejects_result_revision_different_from_task(self) -> None:
        task = self.task()
        result = replace(
            self.valid_result(task),
            issue_revision=task.issue_revision + 1,
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=result,
                current_issue_revision=task.issue_revision,
                received_at=task.created_at,
            )

        self.assertEqual(
            raised.exception.code,
            ValidationCode.ISSUE_REVISION_MISMATCH,
        )

    def test_rejects_payload_that_does_not_match_schema(self) -> None:
        task = self.task(AgentTaskKind.REPRODUCTION)
        result = replace(
            self.valid_result(task),
            payload=ClassificationResult(
                classification="bug",
                confidence=1.0,
                evidence_artifact_ids=(),
            ),
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=result,
                current_issue_revision=task.issue_revision,
                received_at=task.created_at,
            )

        self.assertEqual(
            raised.exception.code,
            ValidationCode.OUTPUT_PAYLOAD_MISMATCH,
        )

    def test_rejects_result_over_output_budget(self) -> None:
        task = replace(
            self.task(),
            budget=AgentBudget(
                wall_seconds=600,
                retry_limit=1,
                max_output_bytes=1,
            ),
        )
        result = AgentResultEnvelope(
            task_id=task.task_id,
            issue_id=task.issue_id,
            issue_revision=task.issue_revision,
            kind=task.kind,
            completed_at=task.created_at,
            output_schema=task.output_schema,
            output_size_bytes=2,
            payload=ClassificationResult(
                classification="bug",
                confidence=1.0,
                evidence_artifact_ids=(),
            ),
        )

        with self.assertRaises(ContractValidationError) as raised:
            validate_result(
                task=task,
                result=result,
                current_issue_revision=task.issue_revision,
                received_at=task.created_at,
            )

        self.assertEqual(raised.exception.code, ValidationCode.INVALID_BUDGET)


class DeterministicMockAgentAdapterTests(AgentContractTestCase):
    def test_returns_identical_result_for_identical_task(self) -> None:
        task = self.task(AgentTaskKind.REPRODUCTION)
        adapter = DeterministicMockAgentAdapter()

        first = adapter.run(task)
        second = adapter.run(task)

        self.assertEqual(first, second)
        self.assertIsInstance(first.payload, ReproductionResult)

    def test_supports_every_allowed_task_kind(self) -> None:
        adapter = DeterministicMockAgentAdapter()

        for kind in AgentTaskKind:
            with self.subTest(kind=kind):
                task = self.task(kind)
                result = adapter.run(task)
                self.assertEqual(result.kind, kind)
                self.assertEqual(result.output_schema, _OUTPUT_SCHEMAS[kind])


if __name__ == "__main__":
    unittest.main()
