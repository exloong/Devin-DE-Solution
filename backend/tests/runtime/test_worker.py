from __future__ import annotations

from app.domain import scenarios
from app.domain.models import JobStatus
from app.domain.states import IssueState
from app.persistence import tables
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
