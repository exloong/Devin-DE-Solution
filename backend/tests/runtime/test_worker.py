from __future__ import annotations

from pathlib import Path

import pytest
from app.domain import scenarios
from app.domain.models import JobKind, JobStatus
from app.domain.states import TARGET_REPOSITORY, EventType, IssueState
from app.integrations import (
    FakeDevinReviewAdapter,
    FakeDevinSessionAdapter,
    FakeGitHubAdapter,
    GitHubCapability,
    PostIssueComment,
    TargetCommit,
)
from app.persistence import tables
from app.runtime.live_worker import LiveWorkerRuntime, live_runtime_from_env
from app.runtime.worker import Worker
from sqlalchemy import select
from tests.conftest import Harness


def test_worker_executes_pending_job_and_updates_heartbeat() -> None:
    harness = Harness()
    issue = harness.open_issue()
    worker = Worker(harness.engine, harness.service, harness.uow, "test-worker")

    assert worker.run_once() == 1
    assert harness.issue(issue.id).state == IssueState.AWAITING_REPORTER
    with harness.uow() as uow:
        jobs = uow.list_jobs(issue_id=issue.id)
    assert sum(job.status == JobStatus.DONE for job in jobs) == 1
    assert all(job.status in {JobStatus.DONE, JobStatus.PENDING} for job in jobs)

    with harness.engine.begin() as conn:
        heartbeat = conn.execute(
            select(tables.runtime_status).where(
                tables.runtime_status.c.component == "worker"
            )
        ).mappings().one()
    assert heartbeat["instance_id"] == "test-worker"
    assert heartbeat["updated_at"] is not None


def test_scenario_seeding_settles_only_jobs_created_by_seed() -> None:
    harness = Harness()
    issue = harness.open_issue(number=999_100)
    with harness.uow() as uow:
        runtime_job = uow.list_jobs(issue_id=issue.id)[0]

    scenarios.seed(harness.uow, harness.service)

    with harness.uow() as uow:
        jobs = uow.list_jobs()
    statuses = {job.id: job.status for job in jobs}
    assert statuses[runtime_job.id] == JobStatus.PENDING
    assert all(
        status == JobStatus.CANCELLED
        for job_id, status in statuses.items()
        if job_id != runtime_job.id
    )


def test_requested_changes_produce_a_new_exact_head() -> None:
    harness = Harness()
    worker = Worker(harness.engine, harness.service, harness.uow, "test-worker")
    issue = harness.classify(harness.open_issue(), missing=[])
    issue = harness.reproduce(issue)
    issue = harness.confirm(issue)

    with harness.uow() as uow:
        first_fix = [
            job for job in uow.list_jobs(issue_id=issue.id) if job.kind == JobKind.START_FIX
        ][-1]
        worker.execute(uow, first_fix)
    first_head = harness.pr(issue.id).head_sha

    harness.route_reviewers(harness.issue(issue.id))
    harness.review(harness.issue(issue.id), state="changes_requested")
    with harness.uow() as uow:
        second_fix = [
            job for job in uow.list_jobs(issue_id=issue.id) if job.kind == JobKind.START_FIX
        ][-1]
        worker.execute(uow, second_fix)

    pr = harness.pr(issue.id)
    assert pr.head_sha != first_head
    assert pr.devin_review_head_sha is None
    assert pr.approved_head_sha is None
    with harness.uow() as uow:
        sessions = uow.list_sessions(issue_id=issue.id)
    assert [session.progress_percent for session in sessions] == [None, 100, 100]


def test_live_worker_binds_sessions_to_revision_and_commit() -> None:
    harness = Harness()
    opened = harness.apply(
        harness.event(
            EventType.ISSUE_OPENED,
            repository=TARGET_REPOSITORY,
            number=100,
            title="Chart export fails",
            body="Steps: open a chart, export CSV.",
            reporter="reporter-1",
            labels=[],
            target_commit="a" * 40,
        )
    )
    assert opened.issue is not None
    issue = opened.issue
    github = FakeGitHubAdapter(branch_head=TargetCommit("a" * 40))
    sessions = FakeDevinSessionAdapter()
    runtime = LiveWorkerRuntime(
        service=harness.service,
        uow_factory=harness.uow,
        github=github,
        devin=sessions,
        review=FakeDevinReviewAdapter(),
        default_branch="master",
    )
    worker = Worker(
        harness.engine,
        harness.service,
        harness.uow,
        "live-worker",
        live_runtime=runtime,
    )

    worker.run_once()

    created = sessions.list_sessions()
    assert len(created) == 1
    assert created[0].target_commit == TargetCommit("a" * 40)
    with harness.uow() as uow:
        stored_session = uow.list_sessions(issue_id=issue.id)[0]
    assert stored_session.issue_revision == issue.revision
    assert stored_session.target_commit == "a" * 40

    sessions.run_to_completion(created[0].session_id)
    worker.run_once()
    worker.run_once()
    reproduction = sessions.list_sessions()[-1]
    sessions.run_to_completion(reproduction.session_id)
    worker.run_once()

    assert harness.issue(issue.id).state == IssueState.NEEDS_OWNER_DECISION
    comments = github.commands_for(GitHubCapability.COMMENT)
    assert len(comments) == 1
    comment = comments[0]
    assert isinstance(comment, PostIssueComment)
    assert "app.devin.ai" not in comment.body
    assert "reproduced" in comment.body.lower()


def test_live_runtime_fails_when_required_credentials_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    for name in (
        "GITHUB_TOKEN",
        "GITHUB_TOKEN_FILE",
        "DEVIN_API_TOKEN",
        "DEVIN_API_TOKEN_FILE",
        "DEVIN_ORG_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        live_runtime_from_env(harness.service, harness.uow)


def test_live_runtime_loads_credentials_from_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    harness = Harness()
    github_token = tmp_path / "github-token"
    devin_token = tmp_path / "devin-token"
    review_token = tmp_path / "review-token"
    github_token.write_text("github-secret\n", encoding="utf-8")
    devin_token.write_text("devin-secret\n", encoding="utf-8")
    review_token.write_text("review-secret\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("DEVIN_API_TOKEN", raising=False)
    monkeypatch.delenv("DEVIN_REVIEW_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(github_token))
    monkeypatch.setenv("DEVIN_API_TOKEN_FILE", str(devin_token))
    monkeypatch.setenv("DEVIN_REVIEW_TOKEN_FILE", str(review_token))
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1234567890abcdef")

    runtime = live_runtime_from_env(harness.service, harness.uow)

    assert runtime.default_branch == "master"
