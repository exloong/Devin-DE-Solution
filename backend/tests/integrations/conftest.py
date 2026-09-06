"""Test fixtures for the Relay integration boundaries.

The integration suite is self-contained: it makes no network calls, requires
no credentials, and adds only the backend package root to ``sys.path`` so it
can run without changing the shared dependency file.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.integrations import (
    AgentCapability,
    CapabilityBudget,
    TargetCommit,
    TaskEnvelope,
    TaskKind,
)

WEBHOOK_SECRET = b"relay-test-secret"
TARGET_SHA = "a" * 40
OTHER_SHA = "b" * 40
TASK_ID = UUID("11111111-1111-1111-1111-111111111111")
ISSUE_ID = UUID("22222222-2222-2222-2222-222222222222")
CREATED_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def make_task(
    kind: TaskKind = TaskKind.CLASSIFICATION,
    *,
    task_id: UUID = TASK_ID,
    issue_revision: int = 3,
    target_commit: str = TARGET_SHA,
    capabilities: frozenset[AgentCapability] | None = None,
    wall_seconds: int = 900,
    max_output_bytes: int = 262_144,
) -> TaskEnvelope:
    """Build a valid task envelope for ``kind`` with policy-compliant limits."""
    default_capabilities = {
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
    return TaskEnvelope(
        task_id=task_id,
        issue_id=ISSUE_ID,
        issue_revision=issue_revision,
        kind=kind,
        created_at=CREATED_AT,
        target_commit=TargetCommit(sha=target_commit),
        budget=CapabilityBudget(
            wall_seconds=wall_seconds, max_output_bytes=max_output_bytes
        ),
        allowed_capabilities=(
            default_capabilities[kind] if capabilities is None else capabilities
        ),
        objective=f"Relay {kind.value} for the reported Superset defect.",
    )


@pytest.fixture()
def classification_task() -> TaskEnvelope:
    return make_task(TaskKind.CLASSIFICATION)


@pytest.fixture()
def fix_task() -> TaskEnvelope:
    return make_task(TaskKind.FIX)
