from __future__ import annotations

from pathlib import Path

import pytest
from app.domain import scenarios
from app.domain.models import JobKind, JobStatus
from app.domain.states import TARGET_REPOSITORY, EventType, IssueState, SessionKind, SessionState
from app.integrations import (
    FakeDevinReviewAdapter,
    FakeDevinSessionAdapter,
    FakeGitHubAdapter,
    GitHubCapability,
    PostIssueComment,
    TargetCommit,
)
from app.integrations.devin_automations import (
    GITHUB_ISSUES_EVENT_TYPE,
    AutomationHandle,
    FakeDevinAutomationClient,
)
from app.integrations.devin_sessions import SessionStatus
from app.integrations.github_client import IssueSnapshot
from app.integrations.json_values import JsonObject
from app.integrations.tasks import TaskKind, TaskPolicy
from app.persistence import automation_registry, runtime_status, tables
from app.persistence.automation_registry import DatabaseAutomationSecretStore
from app.runtime.live_worker import LiveWorkerRuntime, live_runtime_from_env
from app.runtime.worker import Worker
from sqlalchemy import select
from tests.conftest import Harness
from tests.integrations.conftest import make_task


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
        heartbeat = (
            conn.execute(
                select(tables.runtime_status).where(tables.runtime_status.c.component == "worker")
            )
            .mappings()
            .one()
        )
    assert heartbeat["instance_id"] == "test-worker"
    assert heartbeat["updated_at"] is not None


def test_worker_heartbeat_reports_provider_mode() -> None:
    harness = Harness()
    worker = Worker(harness.engine, harness.service, harness.uow, "test-worker")
    worker.heartbeat()
    with harness.engine.connect() as conn:
        rows = runtime_status.read_all(conn)
    assert rows[runtime_status.PROVIDER_GITHUB].instance_id == "dry_run"
    assert rows[runtime_status.PROVIDER_DEVIN].instance_id == "dry_run"

    live = Worker(
        harness.engine,
        harness.service,
        harness.uow,
        "live-worker",
        live_runtime=LiveWorkerRuntime(
            service=harness.service,
            uow_factory=harness.uow,
            github=FakeGitHubAdapter(),
            devin=FakeDevinSessionAdapter(),
            review=FakeDevinReviewAdapter(),
        ),
    )
    live.heartbeat()
    with harness.engine.connect() as conn:
        rows = runtime_status.read_all(conn)
    assert rows[runtime_status.WORKER].instance_id == "live-worker"
    assert rows[runtime_status.PROVIDER_GITHUB].instance_id == "live"
    assert rows[runtime_status.PROVIDER_DEVIN].instance_id == "live"


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


def _issue_snapshot(number: int, *, labels: tuple[str, ...] = ("bug",)) -> IssueSnapshot:
    return IssueSnapshot(
        number=number,
        title="Chart export fails",
        body="Steps: open a chart, export CSV.\nExpected: CSV.\nActual: 500.",
        state="open",
        labels=labels,
        reporter_login="reporter-1",
    )


def _native_output(number: int, *, reproduced: bool = True) -> JsonObject:
    return {
        "issue_number": number,
        "repository": TARGET_REPOSITORY,
        "phase": "done",
        "classification": "bug",
        "context_completeness": 92,
        "rationale": "Deterministic failure with clear steps.",
        "reproduction": {
            "reproduced": reproduced,
            "attempts": 2,
            "observed_behavior": "Export returns HTTP 500.",
            "target_behavior": "Fails on the target commit.",
            "control_behavior": "Passes on the previous release.",
        },
    }


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
    github = FakeGitHubAdapter(
        branch_head=TargetCommit("a" * 40), issues={100: _issue_snapshot(100)}
    )
    sessions = FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400))
    automations = FakeDevinAutomationClient(sessions)
    runtime = LiveWorkerRuntime(
        service=harness.service,
        uow_factory=harness.uow,
        github=github,
        devin=sessions,
        review=FakeDevinReviewAdapter(),
        default_branch="master",
        automations=automations,
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

    # Relay cannot post to the native reproduction automation: the session it
    # created waits until Devin's github:issues trigger starts one for #100.
    with harness.uow() as uow:
        repro = [
            s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.REPRODUCTION
        ][0]
    assert repro.trigger == GITHUB_ISSUES_EVENT_TYPE
    assert repro.automation_id == "auto-fake-reproduction"
    assert repro.external_session_id is None
    assert automations.dispatched(TaskKind.REPRODUCTION) == ()

    native = automations.simulate_native_session(
        make_task(TaskKind.REPRODUCTION, wall_seconds=3600),
        session_id="devin-native-100",
        structured_output=_native_output(100),
        status=SessionStatus.COMPLETED,
    )
    worker.run_once()

    linked = harness.session(repro.id)
    assert linked.external_session_id == native.session_id
    assert linked.state is SessionState.COMPLETED
    assert harness.issue(issue.id).state == IssueState.NEEDS_OWNER_DECISION
    comments = github.commands_for(GitHubCapability.COMMENT)
    assert len(comments) == 1
    comment = comments[0]
    assert isinstance(comment, PostIssueComment)
    assert "app.devin.ai" not in comment.body
    assert "reproduced" in comment.body.lower()


def test_waiting_native_reproduction_expires_after_its_budget() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, _automations = _automation_runtime(harness, auto_spawn=False)
    worker.run_once()
    sessions.run_to_completion(sessions.list_sessions()[0].session_id)
    worker.run_once()
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.REPRODUCING

    harness.service.clock.advance(seconds=3_601)  # type: ignore[attr-defined]
    runtime.sync()
    assert harness.issue(issue.id).state == IssueState.BLOCKED_ENVIRONMENT


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


def _automation_runtime(
    harness: Harness, *, auto_spawn: bool
) -> tuple[LiveWorkerRuntime, Worker, FakeDevinSessionAdapter, FakeDevinAutomationClient]:
    sessions = FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400))
    automations = FakeDevinAutomationClient(sessions, auto_spawn=auto_spawn)
    runtime = LiveWorkerRuntime(
        service=harness.service,
        uow_factory=harness.uow,
        github=FakeGitHubAdapter(
            branch_head=TargetCommit("a" * 40),
            issues={200: _issue_snapshot(200), 201: _issue_snapshot(201)},
        ),
        devin=sessions,
        review=FakeDevinReviewAdapter(),
        default_branch="master",
        automations=automations,
    )
    worker = Worker(harness.engine, harness.service, harness.uow, "live", live_runtime=runtime)
    return runtime, worker, sessions, automations


def _open_with_context(harness: Harness):  # type: ignore[no-untyped-def]
    opened = harness.apply(
        harness.event(
            EventType.ISSUE_OPENED,
            repository=TARGET_REPOSITORY,
            number=200,
            title="Chart export fails",
            body="Steps: open a chart, export CSV.",
            reporter="reporter-1",
            labels=[],
            target_commit="a" * 40,
        )
    )
    assert opened.issue is not None
    return opened.issue


def test_native_session_enrolls_unknown_issue_and_runs_triage_to_owner_decision() -> None:
    """Devin's github:issues trigger fired before Relay saw any webhook."""
    harness = Harness()
    runtime, worker, sessions, automations = _automation_runtime(harness, auto_spawn=False)
    runtime.automation_handles()

    running = automations.simulate_native_session(
        make_task(TaskKind.CLASSIFICATION, wall_seconds=5_400),
        session_id="devin-native-201",
        structured_output={"issue_number": 201, "repository": TARGET_REPOSITORY, "phase": "triage"},
    )
    worker.run_once()

    with harness.uow() as uow:
        repo = uow.get_repository()
        assert repo is not None
        issue = uow.get_issue_by_number(repo.id, 201)
        assert issue is not None
        triage = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind is SessionKind.TRIAGE]
    assert issue.title == "Chart export fails"
    assert issue.reporter_login == "reporter-1"
    assert issue.state == IssueState.TRIAGE
    assert [s.external_session_id for s in triage] == [running.session_id]
    assert triage[0].trigger == GITHUB_ISSUES_EVENT_TYPE
    assert triage[0].progress_source == "devin-automation"
    # Relay's own CLASSIFY job must not start a second classification session.
    assert all(
        s.kind is not TaskKind.CLASSIFICATION or s.session_id == running.session_id
        for s in sessions.list_sessions()
    )
    assert runtime.native_intake.last_polled_at is not None
    assert runtime.native_intake.automation_id == "auto-fake-reproduction"
    with harness.engine.connect() as conn:
        rows = runtime_status.read_all(conn)
    assert rows[runtime_status.DEVIN_AUTOMATION_POLL].instance_id == "auto-fake-reproduction"

    sessions.set_structured_output(running.session_id, _native_output(201), complete=True)
    worker.run_once()

    assert harness.issue(issue.id).state == IssueState.NEEDS_OWNER_DECISION
    with harness.uow() as uow:
        by_kind = {s.kind: s for s in uow.list_sessions(issue_id=issue.id)}
    assert by_kind[SessionKind.TRIAGE].state is SessionState.COMPLETED
    repro = by_kind[SessionKind.REPRODUCTION]
    assert repro.external_session_id == running.session_id
    assert repro.state is SessionState.COMPLETED
    assert automations.dispatched(TaskKind.REPRODUCTION) == ()
    assert automations.dispatched(TaskKind.FIX) == ()


def test_native_session_with_thin_context_asks_the_reporter_instead() -> None:
    harness = Harness()
    runtime, worker, sessions, automations = _automation_runtime(harness, auto_spawn=False)
    runtime.automation_handles()
    automations.simulate_native_session(
        make_task(TaskKind.CLASSIFICATION, wall_seconds=5_400),
        session_id="devin-native-thin",
        structured_output={
            "issue_number": 201,
            "repository": TARGET_REPOSITORY,
            "phase": "done",
            "classification": "bug",
            "context_completeness": 40,
            "missing_fields": [{"field": "version", "prompt": "Which Superset version?"}],
        },
        status=SessionStatus.COMPLETED,
    )
    worker.run_once()
    worker.run_once()
    with harness.uow() as uow:
        repo = uow.get_repository()
        assert repo is not None
        issue = uow.get_issue_by_number(repo.id, 201)
    assert issue is not None
    assert issue.state == IssueState.AWAITING_REPORTER


def test_native_sessions_without_provable_identity_are_never_adopted() -> None:
    harness = Harness()
    runtime, worker, sessions, automations = _automation_runtime(harness, auto_spawn=False)
    runtime.automation_handles()
    task = make_task(TaskKind.CLASSIFICATION, wall_seconds=5_400)
    automations.simulate_native_session(task, session_id="devin-no-output")
    automations.simulate_native_session(
        task,
        session_id="devin-other-repo",
        structured_output={"issue_number": 7, "repository": "other/repo", "phase": "triage"},
        status=SessionStatus.COMPLETED,
    )
    automations.simulate_native_session(
        task,
        session_id="devin-unlabeled",
        structured_output={"issue_number": 202, "repository": TARGET_REPOSITORY, "phase": "triage"},
    )
    runtime.github = FakeGitHubAdapter(
        branch_head=TargetCommit("a" * 40),
        issues={202: _issue_snapshot(202, labels=("question",))},
    )
    worker.run_once()
    with harness.uow() as uow:
        assert uow.list_issues() == []
        assert uow.list_sessions() == []
    assert runtime.native_intake.ignored_session_ids == {"devin-other-repo", "devin-unlabeled"}


def test_relay_started_reproduction_is_bound_to_the_native_session_by_issue() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, automations = _automation_runtime(harness, auto_spawn=False)

    worker.run_once()  # classification still goes through the direct sessions client
    triage = sessions.list_sessions()[0]
    assert triage.kind is TaskKind.CLASSIFICATION
    sessions.run_to_completion(triage.session_id)
    worker.run_once()
    worker.run_once()

    with harness.uow() as uow:
        repro = [
            s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.REPRODUCTION
        ][0]
    assert repro.automation_id == "auto-fake-reproduction"
    assert repro.dispatched_at is not None
    assert repro.external_session_id is None
    assert repro.state is SessionState.RUNNING
    assert automations.dispatched(TaskKind.REPRODUCTION) == ()

    runtime.sync()  # nothing spawned yet: stays pending
    assert harness.session(repro.id).external_session_id is None

    # A native session for a *different* issue must not be taken by FIFO.
    automations.simulate_native_session(
        make_task(TaskKind.REPRODUCTION, wall_seconds=3600),
        session_id="devin-native-other",
        structured_output={"issue_number": 201, "repository": TARGET_REPOSITORY, "phase": "triage"},
    )
    runtime.sync()
    assert harness.session(repro.id).external_session_id is None

    spawned = automations.simulate_native_session(
        make_task(TaskKind.REPRODUCTION, wall_seconds=3600),
        session_id="devin-native-200",
        structured_output={
            "issue_number": 200,
            "repository": TARGET_REPOSITORY,
            "phase": "reproducing",
        },
    )
    runtime.sync()
    linked = harness.session(repro.id)
    assert linked.external_session_id == spawned.session_id
    assert linked.external_session_url == spawned.links.session_url
    assert linked.progress_source == "devin-automation"

    sessions.set_structured_output(spawned.session_id, _native_output(200), complete=True)
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.NEEDS_OWNER_DECISION


def test_owner_authorization_dispatches_the_fix_automation_only() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, automations = _automation_runtime(harness, auto_spawn=True)

    worker.run_once()
    sessions.run_to_completion(sessions.list_sessions()[0].session_id)
    worker.run_once()
    worker.run_once()
    automations.simulate_native_session(
        make_task(TaskKind.REPRODUCTION, wall_seconds=3600),
        session_id="devin-native-200",
        structured_output=_native_output(200),
        status=SessionStatus.COMPLETED,
    )
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.NEEDS_OWNER_DECISION
    assert automations.dispatched(TaskKind.REPRODUCTION) == ()
    assert automations.dispatched(TaskKind.FIX) == ()

    harness.confirm(harness.issue(issue.id))
    worker.run_once()

    with harness.uow() as uow:
        fix = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.FIX][0]
    assert automations.dispatched(TaskKind.FIX) == (str(fix.id),)
    assert fix.automation_id == "auto-fake-fix"
    runtime.sync()
    assert harness.session(fix.id).external_session_id is not None
    assert harness.session(fix.id).external_session_id in {
        s.session_id for s in sessions.list_sessions() if s.kind is TaskKind.FIX
    }


def test_database_secret_store_round_trips_handles_without_leaking_secrets() -> None:
    harness = Harness()
    store = DatabaseAutomationSecretStore(harness.engine, clock=harness.service.clock)
    handle = AutomationHandle(
        automation_id="auto-1",
        kind=TaskKind.FIX,
        inbox_url="https://api.devin.ai/v3/webhooks/inbox/x",
        inbox_secret="s3cret",
    )
    assert store.load(TaskKind.FIX) is None
    store.save(handle)
    assert store.load(TaskKind.FIX) == handle
    store.save(AutomationHandle("auto-2", TaskKind.FIX, handle.inbox_url, "other"))
    assert store.load(TaskKind.FIX).automation_id == "auto-2"  # type: ignore[union-attr]
    with harness.engine.connect() as conn:
        (record,) = automation_registry.read_public(conn)
    assert record.kind == "fix" and record.automation_id == "auto-2"
    assert "s3cret" not in repr(record) and "other" not in repr(record)


def test_live_runtime_always_launches_through_automations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("DEVIN_API_TOKEN", "dv")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1234567890abcdef")
    assert live_runtime_from_env(harness.service, harness.uow).automations is not None
