"""Task and result envelope validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import pytest
from conftest import CREATED_AT, ISSUE_ID, OTHER_SHA, TARGET_SHA, make_task

from app.integrations import (
    SUPERSET_REPOSITORY,
    AgentCapability,
    CapabilityBudget,
    ClassificationOutput,
    ContractValidationError,
    FixOutput,
    PullRequestLink,
    ReproductionOutput,
    ResultEnvelope,
    TargetCommit,
    TaskEnvelope,
    TaskKind,
    TaskPolicy,
    ValidationCode,
    WorkspaceStatus,
    validate_result,
    validate_task,
)

OTHER_TASK_ID = UUID("33333333-3333-3333-3333-333333333333")


def make_result(
    task: TaskEnvelope,
    *,
    payload: object | None = None,
    issue_revision: int | None = None,
    target_commit: str | None = None,
    workspace_status: WorkspaceStatus = WorkspaceStatus.RELEASED,
    completed_offset_seconds: int = 60,
    output_size_bytes: int = 1_024,
    output_schema: str | None = None,
    task_id: UUID | None = None,
) -> ResultEnvelope:
    return ResultEnvelope(
        task_id=task_id or task.task_id,
        issue_id=task.issue_id,
        issue_revision=(
            task.issue_revision if issue_revision is None else issue_revision
        ),
        kind=task.kind,
        session_id="devin-fake-1",
        completed_at=task.created_at + timedelta(seconds=completed_offset_seconds),
        target_commit=TargetCommit(sha=target_commit or task.target_commit.sha),
        workspace_status=workspace_status,
        output_schema=output_schema or task.output_schema,
        output_size_bytes=output_size_bytes,
        payload=(
            payload  # type: ignore[arg-type]
            if payload is not None
            else ClassificationOutput(
                classification="bug", confidence=0.9, rationale="evidence"
            )
        ),
    )


def test_valid_task_passes_policy_validation() -> None:
    validate_task(make_task())


def test_output_schema_is_derived_from_the_task_kind() -> None:
    assert make_task(TaskKind.FIX).output_schema == "fix_result.v1"


def test_task_deadline_follows_its_wall_budget() -> None:
    task = make_task(wall_seconds=600)

    assert task.deadline == CREATED_AT + timedelta(seconds=600)


def test_task_rejects_a_non_utc_creation_time() -> None:
    with pytest.raises(ContractValidationError):
        TaskEnvelope(
            task_id=OTHER_TASK_ID,
            issue_id=ISSUE_ID,
            issue_revision=1,
            kind=TaskKind.CLASSIFICATION,
            created_at=CREATED_AT.replace(tzinfo=None),
            target_commit=TargetCommit(sha=TARGET_SHA),
            budget=CapabilityBudget(wall_seconds=60),
            allowed_capabilities=frozenset({AgentCapability.READ_ISSUE_CONTEXT}),
            objective="triage",
        )


def test_task_rejects_a_repository_other_than_superset() -> None:
    with pytest.raises(ContractValidationError) as error:
        replace(
            make_task(),
            repository="apache/superset",  # type: ignore[arg-type]
        )
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_task_rejects_a_budget_above_policy() -> None:
    task = make_task(wall_seconds=7_200)

    with pytest.raises(ContractValidationError) as error:
        validate_task(task)
    assert error.value.code is ValidationCode.INVALID_BUDGET


def test_budget_values_must_be_positive() -> None:
    with pytest.raises(ContractValidationError):
        CapabilityBudget(wall_seconds=0)
    with pytest.raises(ContractValidationError):
        CapabilityBudget(wall_seconds=60, max_output_bytes=0)


def test_classification_may_not_request_write_capabilities() -> None:
    task = make_task(
        TaskKind.CLASSIFICATION,
        capabilities=frozenset(
            {
                AgentCapability.READ_ISSUE_CONTEXT,
                AgentCapability.WRITE_SCOPED_PATCH,
            }
        ),
    )

    with pytest.raises(ContractValidationError) as error:
        validate_task(task)
    assert error.value.code is ValidationCode.PROHIBITED_CAPABILITY


def test_no_capability_grants_merge_close_or_security_publication() -> None:
    granted = {capability.value for capability in AgentCapability}

    assert granted.isdisjoint(
        {
            "merge_pull_request",
            "close_issue_as_fixed",
            "publish_security_report",
            "execute_reporter_content",
            "write_other_repository",
        }
    )


def test_a_prohibited_capability_string_cannot_enter_a_task() -> None:
    task = make_task(
        capabilities=frozenset({"merge_pull_request"})  # type: ignore[arg-type]
    )

    with pytest.raises(ContractValidationError) as error:
        validate_task(task)
    assert error.value.code is ValidationCode.PROHIBITED_CAPABILITY


def test_matching_result_is_accepted_and_reports_a_released_workspace() -> None:
    task = make_task()

    acceptance = validate_result(
        task=task,
        result=make_result(task),
        current_issue_revision=task.issue_revision,
        received_at=task.created_at + timedelta(seconds=90),
    )

    assert acceptance.task_id == task.task_id
    assert acceptance.workspace_released is True


def test_result_from_another_task_is_rejected() -> None:
    task = make_task()

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, task_id=OTHER_TASK_ID),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.TASK_IDENTITY_MISMATCH


def test_result_for_a_superseded_issue_revision_is_stale() -> None:
    task = make_task(issue_revision=3)

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task),
            current_issue_revision=4,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.STALE_RESULT


def test_result_revision_must_match_its_task_revision() -> None:
    task = make_task(issue_revision=3)

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, issue_revision=2),
            current_issue_revision=3,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.ISSUE_REVISION_MISMATCH


def test_result_against_another_commit_is_rejected() -> None:
    task = make_task()

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, target_commit=OTHER_SHA),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.TARGET_COMMIT_MISMATCH


def test_result_arriving_after_the_deadline_is_late() -> None:
    task = make_task(wall_seconds=120)

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, completed_offset_seconds=60),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=600),
        )
    assert error.value.code is ValidationCode.LATE_RESULT


def test_result_completed_after_the_deadline_is_late() -> None:
    task = make_task(wall_seconds=120)

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, completed_offset_seconds=600),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=61),
        )
    assert error.value.code is ValidationCode.LATE_RESULT


def test_payload_type_must_match_the_task_kind() -> None:
    task = make_task(TaskKind.CLASSIFICATION)
    mismatched = make_result(
        task,
        payload=ReproductionOutput(
            reproduced=True,
            attempts=1,
            observed_behavior="o",
            target_behavior="t",
            control_behavior="c",
        ),
    )

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=mismatched,
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.OUTPUT_PAYLOAD_MISMATCH


def test_result_schema_must_match_the_task_schema() -> None:
    task = make_task()

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, output_schema="classification_result.v2"),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.OUTPUT_SCHEMA_MISMATCH


def test_result_over_its_output_budget_is_rejected() -> None:
    task = make_task(max_output_bytes=512)

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, output_size_bytes=4_096),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.INVALID_BUDGET


def test_result_may_not_report_a_still_active_workspace() -> None:
    task = make_task()

    with pytest.raises(ContractValidationError) as error:
        validate_result(
            task=task,
            result=make_result(task, workspace_status=WorkspaceStatus.ACTIVE),
            current_issue_revision=task.issue_revision,
            received_at=task.created_at + timedelta(seconds=90),
        )
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_fix_result_pull_request_must_target_superset() -> None:
    with pytest.raises(ContractValidationError) as error:
        PullRequestLink(
            repository=SUPERSET_REPOSITORY,
            number=7,
            html_url="https://github.com/apache/superset/pull/7",
            head_branch="relay/fix",
        )
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_fix_result_with_a_superset_pull_request_is_accepted() -> None:
    task = make_task(TaskKind.FIX)
    payload = FixOutput(
        summary="Scoped fix plus regression test.",
        branch_name="relay/fix-40",
        pull_request=PullRequestLink(
            repository=SUPERSET_REPOSITORY,
            number=101,
            html_url="https://github.com/exloong/superset/pull/101",
            head_branch="relay/fix-40",
        ),
        regression_test_paths=("tests/unit_tests/relay_regression_test.py",),
    )

    acceptance = validate_result(
        task=task,
        result=make_result(task, payload=payload),
        current_issue_revision=task.issue_revision,
        received_at=task.created_at + timedelta(seconds=90),
    )

    assert acceptance.kind is TaskKind.FIX


def test_classification_values_and_confidence_are_bounded() -> None:
    with pytest.raises(ContractValidationError):
        ClassificationOutput(classification="fixed", confidence=0.5, rationale="r")
    with pytest.raises(ContractValidationError):
        ClassificationOutput(classification="bug", confidence=1.5, rationale="r")


def test_a_stricter_policy_can_lower_the_capability_allowlist() -> None:
    policy = TaskPolicy(
        allowed_capabilities={
            TaskKind.CLASSIFICATION: frozenset({AgentCapability.READ_ISSUE_CONTEXT})
        }
    )

    with pytest.raises(ContractValidationError) as error:
        validate_task(make_task(TaskKind.CLASSIFICATION), policy)
    assert error.value.code is ValidationCode.PROHIBITED_CAPABILITY
