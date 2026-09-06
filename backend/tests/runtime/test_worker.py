from __future__ import annotations

from pathlib import Path

import pytest
from app.domain import scenarios
from app.domain.models import JobKind, JobStatus
from app.domain.states import (
    TARGET_REPOSITORY,
    ActorRole,
    EventType,
    IssueState,
    SessionKind,
    SessionState,
)
from app.integrations import (
    FakeDevinReviewAdapter,
    FakeDevinSessionAdapter,
    FakeGitHubAdapter,
    GitHubCapability,
    TargetCommit,
)
from app.integrations.devin_automations import (
    GITHUB_ISSUE_COMMENT_EVENT_TYPE,
    GITHUB_ISSUES_EVENT_TYPE,
    AutomationHandle,
    FakeDevinAutomationClient,
)
from app.integrations.devin_sessions import SessionStatus
from app.integrations.github_client import IssueSnapshot
from app.integrations.json_values import JsonObject
from app.integrations.tasks import TaskKind, TaskPolicy
from app.persistence import automation_registry, runtime_status, tables
from app.persistence.automation_registry import DatabaseAutomationRegistry
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


def _issue_snapshot(
    number: int, *, labels: tuple[str, ...] = ("bug",), state: str = "open"
) -> IssueSnapshot:
    return IssueSnapshot(
        number=number,
        title="Chart export fails",
        body="Steps: open a chart, export CSV.\nExpected: CSV.\nActual: 500.",
        state=state,
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


def _fix_output(number: int, *, pr: int | None = 42) -> JsonObject:
    output: JsonObject = {
        "issue_number": number,
        "repository": TARGET_REPOSITORY,
        "phase": "done",
        "summary": "Guarded the export path; regression test added.",
    }
    if pr is None:
        output["blocked_reason"] = "the failing test could not be isolated"
    else:
        output["pull_request"] = {
            "number": pr,
            "url": f"https://github.com/{TARGET_REPOSITORY}/pull/{pr}",
            "head_branch": f"devin/{number}-chart-export",
        }
    return output


def _automation_runtime(
    harness: Harness,
) -> tuple[LiveWorkerRuntime, Worker, FakeDevinSessionAdapter, FakeDevinAutomationClient]:
    sessions = FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400))
    automations = FakeDevinAutomationClient(sessions)
    github = FakeGitHubAdapter(
        branch_head=TargetCommit("a" * 40),
        issues={n: _issue_snapshot(n) for n in (100, 200, 201)},
        pull_request_heads={42: TargetCommit("b" * 40)},
    )
    runtime = LiveWorkerRuntime(
        service=harness.service,
        uow_factory=harness.uow,
        github=github,
        devin=sessions,
        review=FakeDevinReviewAdapter(),
        default_branch="master",
        automations=automations,
        automation_registry=DatabaseAutomationRegistry(harness.engine, clock=harness.service.clock),
    )
    worker = Worker(harness.engine, harness.service, harness.uow, "live", live_runtime=runtime)
    runtime.automation_handles()
    return runtime, worker, sessions, automations


def _open_with_context(harness: Harness, number: int = 200):  # type: ignore[no-untyped-def]
    opened = harness.apply(
        harness.event(
            EventType.ISSUE_OPENED,
            repository=TARGET_REPOSITORY,
            number=number,
            title="Chart export fails",
            body="Steps: open a chart, export CSV.",
            reporter="reporter-1",
            labels=[],
            target_commit="a" * 40,
        )
    )
    assert opened.issue is not None
    return opened.issue


def _native_triage(automations: FakeDevinAutomationClient, number: int, session_id: str):  # type: ignore[no-untyped-def]
    return automations.simulate_native_session(
        make_task(TaskKind.CLASSIFICATION, wall_seconds=5_400),
        session_id=session_id,
        structured_output={
            "issue_number": number,
            "repository": TARGET_REPOSITORY,
            "phase": "triage",
        },
    )


def test_relay_never_starts_sessions_or_comments_for_a_known_issue() -> None:
    """An issue Relay already knows about still waits for Devin's own trigger."""
    harness = Harness()
    issue = _open_with_context(harness, 100)
    runtime, worker, sessions, automations = _automation_runtime(harness)

    worker.run_once()
    worker.run_once()

    assert sessions.list_sessions() == ()
    assert harness.issue(issue.id).state == IssueState.TRIAGE
    assert runtime.github.commands_for(GitHubCapability.COMMENT) == ()  # type: ignore[attr-defined]

    running = _native_triage(automations, 100, "devin-native-100")
    worker.run_once()
    with harness.uow() as uow:
        triage = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind is SessionKind.TRIAGE]
    assert [s.external_session_id for s in triage] == [running.session_id]
    assert triage[0].issue_revision == issue.revision
    assert triage[0].target_commit == "a" * 40
    assert triage[0].trigger == GITHUB_ISSUES_EVENT_TYPE

    sessions.set_structured_output(running.session_id, _native_output(100), complete=True)
    worker.run_once()
    worker.run_once()

    assert harness.issue(issue.id).state == IssueState.FIXING
    with harness.uow() as uow:
        decisions = uow.list_decisions(issue_id=issue.id)
        by_kind = {s.kind: s for s in uow.list_sessions(issue_id=issue.id)}
    assert all(d.actor.role is not ActorRole.OWNER for d in decisions)
    assert by_kind[SessionKind.REPRODUCTION].external_session_id == running.session_id
    fix = by_kind[SessionKind.FIX]
    assert fix.trigger == GITHUB_ISSUE_COMMENT_EVENT_TYPE
    assert fix.automation_id == "auto-fake-fix"
    assert fix.external_session_id is None
    assert runtime.github.commands_for(GitHubCapability.COMMENT) == ()  # type: ignore[attr-defined]
    assert [s.kind for s in sessions.list_sessions()] == [TaskKind.CLASSIFICATION]


def test_waiting_native_fix_expires_after_its_budget() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, automations = _automation_runtime(harness)
    running = _native_triage(automations, 200, "devin-native-200")
    worker.run_once()
    sessions.set_structured_output(running.session_id, _native_output(200), complete=True)
    worker.run_once()
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.FIXING

    harness.service.clock.advance(seconds=5_401)  # type: ignore[attr-defined]
    runtime.sync()
    assert harness.issue(issue.id).state == IssueState.AUTOMATION_ERROR


def test_native_session_enrolls_unknown_issue_and_runs_triage_to_owner_decision() -> None:
    """Devin's github:issues trigger fired before Relay saw any webhook."""
    harness = Harness()
    runtime, worker, sessions, automations = _automation_runtime(harness)
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

    assert harness.issue(issue.id).state == IssueState.FIX_PENDING
    with harness.uow() as uow:
        by_kind = {s.kind: s for s in uow.list_sessions(issue_id=issue.id)}
    assert by_kind[SessionKind.TRIAGE].state is SessionState.COMPLETED
    repro = by_kind[SessionKind.REPRODUCTION]
    assert repro.external_session_id == running.session_id
    assert repro.state is SessionState.COMPLETED
    assert not hasattr(automations, "dispatch")


def test_native_session_with_thin_context_asks_the_reporter_instead() -> None:
    harness = Harness()
    runtime, worker, sessions, automations = _automation_runtime(harness)
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
    runtime, worker, sessions, automations = _automation_runtime(harness)
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
        issues={202: _issue_snapshot(202, labels=("question",), state="closed")},
    )
    worker.run_once()
    with harness.uow() as uow:
        assert uow.list_issues() == []
        assert uow.list_sessions() == []
    assert runtime.native_intake.ignored_session_ids == {"devin-other-repo", "devin-unlabeled"}


def test_native_fix_session_is_adopted_by_issue_and_records_the_pull_request() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, automations = _automation_runtime(harness)
    running = _native_triage(automations, 200, "devin-native-200")
    worker.run_once()
    sessions.set_structured_output(running.session_id, _native_output(200), complete=True)
    worker.run_once()
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.FIXING

    # A fix session for another issue is never taken by FIFO.
    automations.simulate_native_session(
        make_task(TaskKind.FIX, wall_seconds=3600),
        session_id="devin-fix-other",
        kind=TaskKind.FIX,
        structured_output={"issue_number": 201, "repository": TARGET_REPOSITORY, "phase": "fixing"},
    )
    runtime.sync()
    with harness.uow() as uow:
        fix = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind is SessionKind.FIX][0]
    assert fix.external_session_id is None

    spawned = automations.simulate_native_session(
        make_task(TaskKind.FIX, wall_seconds=3600),
        session_id="devin-fix-200",
        kind=TaskKind.FIX,
        structured_output={"issue_number": 200, "repository": TARGET_REPOSITORY, "phase": "fixing"},
    )
    runtime.sync()
    linked = harness.session(fix.id)
    assert linked.external_session_id == spawned.session_id
    assert linked.trigger == GITHUB_ISSUE_COMMENT_EVENT_TYPE
    assert linked.progress_source == "devin-automation"

    sessions.set_structured_output(spawned.session_id, _fix_output(200), complete=True)
    worker.run_once()

    assert harness.session(fix.id).state is SessionState.COMPLETED
    pr = harness.pr(issue.id)
    assert pr.number == 42
    assert pr.head_branch == "devin/200-chart-export"
    assert runtime.github.commands_for(GitHubCapability.COMMENT) == ()  # type: ignore[attr-defined]
    assert runtime.github.commands_for(GitHubCapability.PULL_REQUEST) == ()  # type: ignore[attr-defined]
    assert harness.issue(issue.id).state == IssueState.PR_OPEN
    with harness.engine.connect() as conn:
        records = {r.kind: r.automation_id for r in automation_registry.read_public(conn)}
    assert records == {"fix": "auto-fake-fix", "reproduction": "auto-fake-reproduction"}


def test_blocked_native_fix_surfaces_as_an_automation_error() -> None:
    harness = Harness()
    issue = _open_with_context(harness)
    runtime, worker, sessions, automations = _automation_runtime(harness)
    running = _native_triage(automations, 200, "devin-native-200")
    worker.run_once()
    sessions.set_structured_output(running.session_id, _native_output(200), complete=True)
    worker.run_once()
    worker.run_once()
    automations.simulate_native_session(
        make_task(TaskKind.FIX, wall_seconds=3600),
        session_id="devin-fix-200",
        kind=TaskKind.FIX,
        structured_output=_fix_output(200, pr=None),
        status=SessionStatus.COMPLETED,
    )
    worker.run_once()
    assert harness.issue(issue.id).state == IssueState.AUTOMATION_ERROR


def test_database_registry_round_trips_automation_ids() -> None:
    harness = Harness()
    store = DatabaseAutomationRegistry(harness.engine, clock=harness.service.clock)
    handle = AutomationHandle(automation_id="auto-1", kind=TaskKind.FIX)
    assert store.load(TaskKind.FIX) is None
    store.save(handle)
    assert store.load(TaskKind.FIX) == handle
    store.save(AutomationHandle("auto-2", TaskKind.FIX, enabled=False))
    assert store.load(TaskKind.FIX) == AutomationHandle("auto-2", TaskKind.FIX, enabled=False)
    with harness.engine.connect() as conn:
        (record,) = automation_registry.read_public(conn)
    assert record.kind == "fix" and record.automation_id == "auto-2"
    assert "inbox" not in repr(record)


def test_live_runtime_always_launches_through_automations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness()
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("DEVIN_API_TOKEN", "dv")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-1234567890abcdef")
    assert live_runtime_from_env(harness.service, harness.uow).automations is not None
