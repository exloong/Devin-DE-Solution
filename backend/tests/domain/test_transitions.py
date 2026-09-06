from __future__ import annotations

import uuid
from datetime import timedelta
from types import TracebackType

import pytest
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import AttemptStatus, JobKind, JobStatus, QuestionStatus
from app.domain.states import (
    ACTIVE_AGENT_STATES,
    TARGET_REPOSITORY,
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    ActorRole,
    EventType,
    IssueState,
    SessionKind,
    SessionState,
    TransitionName,
)

from tests.conftest import Harness

MISSING = [{"field": "logs", "prompt": "Attach redacted logs."}]


class expect:
    """Context manager asserting a DomainError with a specific code."""

    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        self._ctx = pytest.raises(DomainError)

    def __enter__(self) -> None:
        self._info = self._ctx.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        result = self._ctx.__exit__(exc_type, exc, tb)
        assert self._info.value.code == self.code, self._info.value
        return result


def test_transition_table_is_consistent() -> None:
    for name, spec in TRANSITIONS.items():
        assert spec.name == name
        assert spec.events and spec.sources and spec.destinations and spec.actors
        for dest in spec.destinations:
            assert dest != IssueState.NEW
    assert WAITING_STATES.isdisjoint({IssueState.REPRODUCING, IssueState.FIXING})
    assert IssueState.COMPLETED in TERMINAL_STATES


def test_intake_creates_issue_in_triage_and_enqueues_classify(h: Harness) -> None:
    issue = h.open_issue()
    assert issue.state == IssueState.TRIAGE
    assert issue.version == 1
    with h.uow() as uow:
        jobs = uow.list_jobs(issue.id)
        repo = uow.get_repository()
    assert [j.kind for j in jobs] == [JobKind.CLASSIFY]
    assert repo is not None and repo.full_name == TARGET_REPOSITORY


def test_repository_scope_is_enforced_on_intake(h: Harness) -> None:
    with expect(ErrorCode.REPOSITORY_NOT_ALLOWED):
        h.apply(
            h.event(
                EventType.ISSUE_OPENED, repository="apache/superset", number=1, title="x", body=""
            )
        )


def test_duplicate_delivery_is_noop_and_creates_no_jobs(h: Harness) -> None:
    issue = h.open_issue(delivery_id="gh-1")
    again = h.apply(
        h.event(
            EventType.ISSUE_OPENED,
            delivery_id="gh-1",
            repository=TARGET_REPOSITORY,
            number=100,
            title="Chart export fails",
            body="",
        )
    )
    assert again.duplicate is True
    assert again.issue is not None and again.issue.version == issue.version
    with h.uow() as uow:
        assert len(uow.list_jobs(issue.id)) == 1
        assert len(uow.list_attempts(issue.id)) == 1
        assert len(uow.list_issues()) == 1


def test_same_issue_number_cannot_be_opened_twice(h: Harness) -> None:
    h.open_issue()
    with expect(ErrorCode.ILLEGAL_TRANSITION):
        h.open_issue(delivery_id="other")


def test_missing_evidence_requests_information(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    assert issue.state == IssueState.AWAITING_REPORTER
    with h.uow() as uow:
        kinds = {j.kind for j in uow.list_jobs(issue.id)}
        sessions = uow.list_sessions(issue_id=issue.id)
    assert {JobKind.PUBLISH_QUESTIONS, JobKind.REMINDER, JobKind.INACTIVITY} <= kinds
    assert sessions == []


def test_unavailable_evidence_cannot_become_reproduction_ready(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    result = h.apply(
        h.event(
            EventType.REPORTER_RESPONSE,
            issue_id=issue.id,
            role=ActorRole.REPORTER,
            login="reporter-1",
            answers=[{"field": "logs", "unavailable": True}],
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.AWAITING_REPORTER
    with expect(ErrorCode.MISSING_EVIDENCE):
        h.apply(h.event(EventType.REPRODUCTION_STARTED, issue_id=issue.id))
    with h.uow() as uow:
        questions = uow.list_questions(issue.id)
        kinds = {j.kind for j in uow.list_jobs(issue.id)}
    assert questions[0].status == QuestionStatus.UNAVAILABLE
    assert JobKind.SAFE_ALTERNATIVE_REVIEW in kinds
    assert JobKind.START_REPRODUCTION not in kinds


def test_reporter_answers_require_original_reporter_or_audited_override(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    answers = [{"field": "logs", "answer": "trace attached"}]
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.apply(
            h.event(
                EventType.REPORTER_RESPONSE,
                issue_id=issue.id,
                role=ActorRole.REPORTER,
                login="someone-else",
                answers=answers,
            )
        )
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.apply(
            h.event(
                EventType.REPORTER_RESPONSE,
                issue_id=issue.id,
                role=ActorRole.OPERATOR,
                login="ops-1",
                answers=answers,
            )
        )
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.apply(
            h.event(
                EventType.REPORTER_RESPONSE,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                login="devin",
                answers=answers,
            )
        )
    result = h.apply(
        h.event(
            EventType.REPORTER_RESPONSE,
            issue_id=issue.id,
            role=ActorRole.OPERATOR,
            login="ops-1",
            answers=answers,
            override_rationale="reporter pasted logs in a private channel",
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.REPRODUCING
    with h.uow() as uow:
        audits = [
            a
            for a in uow.list_audit(issue.correlation_id)
            if a.action == "override:reporter_response"
        ]
    assert len(audits) == 1 and audits[0].actor.login == "ops-1"


def test_fix_result_pr_url_must_match_scoped_repository(h: Harness) -> None:
    issue = h.start_fix(h.confirm(h.reproduce(h.classify(h.open_issue()))))
    with h.uow() as uow:
        session = next(s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.FIX)
    bad_urls = [
        "https://github.com/evil/superset/pull/501",
        "https://github.com/exloong/superset/pull/999",
        "https://github.com/exloong/superset/pulls/501",
        "http://github.com/exloong/superset/pull/501",
    ]
    for url in bad_urls:
        with expect(ErrorCode.REPOSITORY_NOT_ALLOWED):
            h.apply(
                h.event(
                    EventType.FIX_RESULT,
                    issue_id=issue.id,
                    role=ActorRole.AGENT,
                    login="devin",
                    revision=issue.revision,
                    session_id=str(session.id),
                    pull_request={
                        "number": 501,
                        "head_branch": "fix",
                        "head_sha": "a" * 40,
                        "url": url,
                    },
                )
            )


def test_answered_evidence_starts_reproduction_with_live_workspace(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    result = h.apply(
        h.event(
            EventType.REPORTER_RESPONSE,
            issue_id=issue.id,
            role=ActorRole.REPORTER,
            login="reporter-1",
            answers=[{"field": "logs", "answer": "rm -rf / ; <script>alert(1)</script>"}],
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.REPRODUCING
    with h.uow() as uow:
        session = uow.list_sessions(issue_id=issue.id)[0]
        question = uow.list_questions(issue.id)[0]
    assert session.workspace_live is True
    assert session.repository == TARGET_REPOSITORY
    assert question.answer is not None and question.answer.startswith("rm -rf")


def test_security_label_fails_closed_at_intake(h: Harness) -> None:
    issue = h.open_issue(labels=["security"])
    assert issue.state == IssueState.SECURITY_PRIVATE
    assert issue.security_flagged is True
    with h.uow() as uow:
        kinds = [j.kind for j in uow.list_jobs(issue.id)]
    assert kinds == [JobKind.PRIVATE_SECURITY_TASK]
    with expect(ErrorCode.SECURITY_FAIL_CLOSED):
        h.apply(h.event(EventType.REPRODUCTION_STARTED, issue_id=issue.id))


def test_security_text_signal_cancels_public_work(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    assert issue.state == IssueState.REPRODUCING
    result = h.apply(
        h.event(
            EventType.SECURITY_SIGNAL,
            issue_id=issue.id,
            reason="reporter mentions CVE-2026-0001 exploit",
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.SECURITY_PRIVATE
    with h.uow() as uow:
        sessions = uow.list_sessions(issue_id=issue.id)
        jobs = uow.list_jobs(issue.id)
    assert all(not s.workspace_live for s in sessions)
    assert sessions[0].state == SessionState.CANCELLED
    live = [j for j in jobs if j.status in (JobStatus.PENDING, JobStatus.CLAIMED)]
    assert [j.kind for j in live] == [JobKind.PRIVATE_SECURITY_TASK]


def _statuses(h: Harness, issue_id: uuid.UUID, kind: JobKind) -> list[str]:
    with h.uow() as uow:
        return [j.status.value for j in uow.list_jobs(issue_id) if j.kind == kind]


def test_security_routing_cancels_non_public_kinds_too(h: Harness) -> None:
    # needs_owner_decision schedules ESCALATE_OWNER (not in PUBLIC_JOB_KINDS);
    # awaiting_reporter schedules REMINDER/INACTIVITY timers. All must die.
    issue = h.reproduce(h.classify(h.open_issue()))
    assert len(h.jobs(issue.id, JobKind.ESCALATE_OWNER)) == 1
    waiting = h.classify(
        h.open_issue(number=101), missing=[{"field": "feature_flags", "prompt": "Which flags?"}]
    )
    assert len(h.jobs(waiting.id, JobKind.INACTIVITY)) == 1
    for target in (issue, waiting):
        routed = h.apply(
            h.event(EventType.SECURITY_SIGNAL, issue_id=target.id, reason="private disclosure")
        )
        assert routed.issue is not None and routed.issue.state == IssueState.SECURITY_PRIVATE
        with h.uow() as uow:
            jobs = uow.list_jobs(target.id)
        outstanding = {j.kind for j in jobs if j.status in (JobStatus.PENDING, JobStatus.CLAIMED)}
        assert outstanding == {JobKind.PRIVATE_SECURITY_TASK}
        assert all(j.finished_at is not None for j in jobs if j.status == JobStatus.CANCELLED)


def test_claimed_public_job_is_rechecked_before_side_effects(h: Harness) -> None:
    """Race: a worker claims PUBLISH_QUESTIONS, then the issue turns security-private."""
    issue = h.classify(
        h.open_issue(), missing=[{"field": "feature_flags", "prompt": "Which flags?"}]
    )
    with h.uow() as uow:
        job = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.PUBLISH_QUESTIONS)
    with h.uow() as uow:
        claimed = h.service.claim_job(uow, job.id, "worker-a")
    assert claimed.status == JobStatus.CLAIMED and claimed.claimed_by == "worker-a"
    with h.uow() as uow, expect(ErrorCode.ILLEGAL_TRANSITION):
        h.service.claim_job(uow, job.id, "worker-b")
    h.apply(h.event(EventType.SECURITY_SIGNAL, issue_id=issue.id, reason="exploit details"))
    with h.uow() as uow:
        assert uow.get_job(job.id) is not None
        cancelled = uow.get_job(job.id)
    assert cancelled is not None and cancelled.status == JobStatus.CANCELLED
    effects: list[str] = []
    with h.uow() as uow, expect(ErrorCode.PROHIBITED_ACTION):
        with h.service.run_claimed_job(uow, job.id, "worker-a"):
            effects.append("published")
    assert effects == []
    assert _statuses(h, issue.id, JobKind.PUBLISH_QUESTIONS) == ["cancelled"]


def test_claimed_job_rejected_when_issue_became_private_without_cancel(h: Harness) -> None:
    """Even if cancellation were missed, completion rechecks the security flag."""
    issue = h.classify(
        h.open_issue(), missing=[{"field": "feature_flags", "prompt": "Which flags?"}]
    )
    with h.uow() as uow:
        job = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.PUBLISH_QUESTIONS)
        h.service.claim_job(uow, job.id, "worker-a")
    with h.uow() as uow:
        found = uow.get_issue(issue.id)
        assert found is not None
        found.security_flagged = True
        uow.save_issue(found)
        uow.commit()
    effects: list[str] = []
    with h.uow() as uow, expect(ErrorCode.SECURITY_FAIL_CLOSED):
        with h.service.run_claimed_job(uow, job.id, "worker-a"):
            effects.append("published")
    assert effects == []
    assert _statuses(h, issue.id, JobKind.PUBLISH_QUESTIONS) == ["cancelled"]


def test_stale_revision_job_cannot_run(h: Harness) -> None:
    issue = h.classify(
        h.open_issue(), missing=[{"field": "feature_flags", "prompt": "Which flags?"}]
    )
    with h.uow() as uow:
        job = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.PUBLISH_QUESTIONS)
        h.service.claim_job(uow, job.id, "worker-a")
        found = uow.get_issue(issue.id)
        assert found is not None
        found.revision += 1
        uow.save_issue(found)
        uow.commit()
    with h.uow() as uow, expect(ErrorCode.STALE_RESULT):
        with h.service.run_claimed_job(uow, job.id, "worker-a"):
            raise AssertionError("effect must not run for a stale job")
    assert _statuses(h, issue.id, JobKind.PUBLISH_QUESTIONS) == ["cancelled"]


def test_claim_and_complete_happy_path_and_not_due(h: Harness) -> None:
    issue = h.reproduce(h.classify(h.open_issue()))
    with h.uow() as uow:
        escalate = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.ESCALATE_OWNER)
        start = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.START_REPRODUCTION)
    with h.uow() as uow, expect(ErrorCode.ILLEGAL_TRANSITION):
        h.service.claim_job(uow, escalate.id, "w")
    with h.uow() as uow:
        h.service.claim_job(uow, start.id, "w")
        with h.service.run_claimed_job(uow, start.id, "w") as running:
            assert running.status == JobStatus.CLAIMED
            done = uow.get_job(start.id)
            assert done is not None and done.status == JobStatus.CLAIMED
        done = uow.get_job(start.id)
    assert done is not None and done.status == JobStatus.DONE and done.finished_at is not None
    with h.uow() as uow, expect(ErrorCode.ILLEGAL_TRANSITION):
        with h.service.run_claimed_job(uow, start.id, "w"):
            raise AssertionError("finished job must not run again")


def test_failed_side_effect_is_never_done_and_retries_with_backoff(h: Harness) -> None:
    issue = h.reproduce(h.classify(h.open_issue()))
    with h.uow() as uow:
        start = next(j for j in uow.list_jobs(issue.id) if j.kind == JobKind.START_REPRODUCTION)
    assert start.max_attempts == 3
    for attempt in range(1, start.max_attempts + 1):
        with h.uow() as uow:
            claimed = h.service.claim_job(uow, start.id, "w")
        assert claimed.attempts == attempt
        with h.uow() as uow, pytest.raises(ConnectionError):
            with h.service.run_claimed_job(uow, start.id, "w"):
                raise ConnectionError("github unavailable")
        with h.uow() as uow:
            after = uow.get_job(start.id)
        assert after is not None and after.claimed_by is None
        if attempt < start.max_attempts:
            assert after.status == JobStatus.PENDING
            expected_delay = timedelta(seconds=30 * 2 ** (attempt - 1))
            assert after.run_after == h.clock.now() + expected_delay
            with h.uow() as uow, expect(ErrorCode.ILLEGAL_TRANSITION):
                h.service.claim_job(uow, start.id, "w")
            h.clock.current += expected_delay
        else:
            assert after.status == JobStatus.FAILED and after.finished_at is not None
    with h.uow() as uow, expect(ErrorCode.ILLEGAL_TRANSITION):
        h.service.claim_job(uow, start.id, "w")


def test_confirm_bug_creates_exactly_one_fix_session(h: Harness) -> None:
    issue = h.confirm(h.reproduce(h.classify(h.open_issue())))
    with h.uow() as uow:
        fixes = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.FIX]
        start_jobs = [j for j in uow.list_jobs(issue.id) if j.kind == JobKind.START_FIX]
    assert len(fixes) == 1 and fixes[0].state == SessionState.QUEUED
    assert len(start_jobs) == 1 and start_jobs[0].payload["session_id"] == str(fixes[0].id)
    started = h.start_fix(issue)
    assert started.state == IssueState.FIXING
    with h.uow() as uow:
        fixes = [s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.FIX]
    assert len(fixes) == 1 and fixes[0].state == SessionState.RUNNING
    # A second start has no queued session to start.
    with expect(ErrorCode.ILLEGAL_TRANSITION):
        h.apply(h.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id))
    with h.uow() as uow:
        assert (
            len([s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.FIX]) == 1
        )


def test_fix_session_start_requires_matching_queued_session(h: Harness) -> None:
    issue = h.confirm(h.reproduce(h.classify(h.open_issue())))
    with h.uow() as uow:
        repro = next(
            s for s in uow.list_sessions(issue_id=issue.id) if s.kind == SessionKind.REPRODUCTION
        )
    with expect(ErrorCode.INVALID_INPUT):
        h.apply(h.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id, session_id=str(repro.id)))
    with expect(ErrorCode.NOT_FOUND):
        h.apply(
            h.event(
                EventType.FIX_SESSION_STARTED, issue_id=issue.id, session_id=str(uuid.UUID(int=9))
            )
        )
    assert h.issue(issue.id).state == IssueState.FIX_AUTHORIZED


def test_confirm_bug_required_before_fix(h: Harness) -> None:
    issue = h.reproduce(h.classify(h.open_issue()))
    assert issue.state == IssueState.NEEDS_OWNER_DECISION
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(h.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id))


def test_agent_cannot_confirm_bug(h: Harness) -> None:
    issue = h.reproduce(h.classify(h.open_issue()))
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(
            h.event(
                EventType.OWNER_DECISION,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                decision="confirm_bug",
            )
        )


def test_confirm_bug_authorizes_and_expires(h: Harness) -> None:
    issue = h.confirm(h.reproduce(h.classify(h.open_issue())))
    assert issue.state == IssueState.FIX_AUTHORIZED
    h.clock.advance(days=8)
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.start_fix(issue)


def test_waiting_states_never_report_live_workspace(h: Harness) -> None:
    issue = h.to_pr_open()
    assert issue.state == IssueState.PR_OPEN
    with h.uow() as uow:
        sessions = uow.list_sessions(issue_id=issue.id)
    assert len(sessions) == 2
    assert all(s.workspace_released and not s.workspace_live for s in sessions)
    with expect(ErrorCode.ILLEGAL_TRANSITION):
        h.apply(
            h.event(
                EventType.SESSION_PROGRESS,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                session_id=str(sessions[-1].id),
                progress_percent=50,
            )
        )


def test_fix_pr_outside_scope_is_rejected(h: Harness) -> None:
    issue = h.start_fix(h.confirm(h.reproduce(h.classify(h.open_issue()))))
    with expect(ErrorCode.REPOSITORY_NOT_ALLOWED):
        h.open_pr(issue, repository="exloong/other-repo")
    assert h.issue(issue.id).state == IssueState.FIXING


def test_devin_review_is_not_human_approval(h: Harness) -> None:
    issue = h.to_routed_pr()
    pr = h.pr(issue.id)
    result = h.apply(
        h.event(
            EventType.DEVIN_REVIEW_COMPLETED,
            issue_id=issue.id,
            role=ActorRole.AGENT,
            pr_number=pr.number,
            head_sha=pr.head_sha,
            verdict="passed",
            findings=0,
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.AWAITING_OWNER
    pr = h.pr(issue.id)
    assert pr.devin_review.value == "passed"
    assert pr.devin_review_head_sha == pr.head_sha
    assert pr.human_approved is False
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.merge(issue)
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.review(issue, role=ActorRole.AGENT, login="devin")


def test_human_approval_then_merge_completes(h: Harness) -> None:
    issue = h.to_routed_pr()
    approved = h.review(issue)
    assert approved.issue is not None and approved.issue.state == IssueState.AWAITING_OWNER
    pr = h.pr(issue.id)
    assert pr.approved_head_sha == pr.head_sha and pr.approval_override is False
    merged = h.merge(issue)
    assert merged.issue is not None and merged.issue.state == IssueState.COMPLETED
    assert merged.attempt.transition == TransitionName.COMPLETE


def test_changes_requested_resumes_fix_without_new_confirmation(h: Harness) -> None:
    issue = h.to_routed_pr()
    pr = h.pr(issue.id)
    changed = h.apply(
        h.event(
            EventType.OWNER_DECISION,
            issue_id=issue.id,
            role=ActorRole.OWNER,
            login="owner-1",
            decision="request_changes",
            rationale="tighten test",
            pr_number=pr.number,
            head_sha=pr.head_sha,
        )
    )
    assert changed.issue is not None and changed.issue.state == IssueState.CHANGES_REQUESTED
    resumed = h.start_fix(changed.issue)
    assert resumed.state == IssueState.FIXING


# --------------------------------------------------- trusted reviewer routing


def test_agent_fix_result_cannot_choose_reviewers(h: Harness) -> None:
    issue = h.start_fix(h.confirm(h.reproduce(h.classify(h.open_issue()))))
    with expect(ErrorCode.PROHIBITED_ACTION):
        h.open_pr(issue, reviewer_candidates=["attacker"])
    assert h.issue(issue.id).state == IssueState.FIXING
    with h.uow() as uow:
        assert uow.list_pull_requests(issue.id) == []


def test_routing_must_come_from_trusted_adapter(h: Harness) -> None:
    issue = h.to_pr_open()
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.route_reviewers(issue, role=ActorRole.AGENT, members=["attacker"])
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.route_reviewers(issue, role=ActorRole.OWNER, members=["attacker"])
    with h.uow() as uow:
        assert uow.list_reviewer_routings(issue.id) == []


def test_owner_role_alone_cannot_approve_without_routing(h: Harness) -> None:
    issue = h.to_pr_open()
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.review(issue, login="owner-1")
    h.route_reviewers(issue, members=["real-owner"])
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.review(issue, login="owner-1")
    ok = h.review(issue, login="real-owner")
    assert ok.issue is not None and ok.issue.state == IssueState.AWAITING_OWNER
    with h.uow() as uow:
        routing = uow.list_reviewer_routings(issue.id)[-1]
    assert routing.state.value == "resolved"
    assert routing.rationale == "deterministic CODEOWNERS match"
    assert h.pr(issue.id).routing_id == routing.id


def test_unresolved_routing_escalates_and_blocks_owner_approval(h: Harness) -> None:
    issue = h.to_pr_open()
    result = h.route_reviewers(issue, state="no_owner", members=[])
    assert result.attempt.status == AttemptStatus.NOOP
    assert h.jobs(issue.id, JobKind.REQUEST_REVIEWERS) == []
    assert any("routing:" in key for key in h.jobs(issue.id, JobKind.ESCALATE_OWNER))
    with h.uow() as uow:
        routing = uow.list_reviewer_routings(issue.id)[-1]
    assert routing.unowned_paths == ["docs/new.md"]
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.review(issue, login="owner-1")


def test_operator_override_is_explicit_and_audited(h: Harness) -> None:
    issue = h.to_pr_open()
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.review(issue, role=ActorRole.OPERATOR, login="ops")
    ok = h.review(
        issue, role=ActorRole.OPERATOR, login="ops", override=True, rationale="owner on leave"
    )
    assert ok.issue is not None and ok.issue.state == IssueState.AWAITING_OWNER
    pr = h.pr(issue.id)
    assert pr.approval_override is True and pr.human_approver == "ops"


# ------------------------------------------------------- PR head binding


def test_review_and_approval_bind_to_exact_head(h: Harness) -> None:
    issue = h.to_routed_pr()
    pr = h.pr(issue.id)
    with expect(ErrorCode.STALE_RESULT):
        h.apply(
            h.event(
                EventType.DEVIN_REVIEW_COMPLETED,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                pr_number=pr.number,
                head_sha="0ldhead",
                verdict="passed",
            )
        )
    with expect(ErrorCode.STALE_RESULT):
        h.review(issue, head_sha="0ldhead")
    with expect(ErrorCode.INVALID_INPUT):
        h.apply(
            h.event(EventType.PR_MERGED, issue_id=issue.id, pr_number=999, head_sha=pr.head_sha)
        )


def test_synchronize_invalidates_approval_and_requeues_review(h: Harness) -> None:
    issue = h.to_routed_pr()
    h.review(issue)
    pr = h.pr(issue.id)
    assert pr.approval_binds(pr.head_sha)
    synced = h.apply(
        h.event(
            EventType.PR_SYNCHRONIZED, issue_id=issue.id, pr_number=pr.number, head_sha="newhead"
        )
    )
    assert synced.attempt.transition == TransitionName.PR_HEAD_UPDATED
    assert synced.issue is not None and synced.issue.state == IssueState.PR_OPEN
    pr = h.pr(issue.id)
    assert pr.head_sha == "newhead"
    assert pr.human_approved is False and pr.approved_head_sha is None
    assert pr.devin_review.value == "pending" and pr.routing_id is None
    assert any(key.endswith("newhead") for key in h.jobs(issue.id, JobKind.TRIGGER_DEVIN_REVIEW))
    assert any(key.endswith("newhead") for key in h.jobs(issue.id, JobKind.RESOLVE_REVIEWERS))
    # stale merge for the old head cannot complete; the new head has no approval
    with expect(ErrorCode.STALE_RESULT):
        h.merge(issue, head_sha="deadbeef")
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.merge(issue)
    # approval for the old head cannot be replayed
    with expect(ErrorCode.STALE_RESULT):
        h.review(issue, head_sha="deadbeef")
    # old routing does not authorize the new head
    with expect(ErrorCode.UNAUTHORIZED_ACTOR):
        h.review(issue)
    h.route_reviewers(issue)
    h.review(issue)
    done = h.merge(issue)
    assert done.issue is not None and done.issue.state == IssueState.COMPLETED


def test_synchronize_with_same_head_is_noop(h: Harness) -> None:
    issue = h.to_routed_pr()
    pr = h.pr(issue.id)
    result = h.apply(
        h.event(
            EventType.PR_SYNCHRONIZED, issue_id=issue.id, pr_number=pr.number, head_sha=pr.head_sha
        )
    )
    assert result.attempt.status == AttemptStatus.NOOP


def test_stale_session_result_cannot_advance_newer_revision(h: Harness) -> None:
    issue = h.reproduce(h.classify(h.open_issue()))
    old_session = h.latest_session_id(issue.id)
    h.apply(
        h.event(
            EventType.OWNER_DECISION,
            issue_id=issue.id,
            role=ActorRole.OWNER,
            decision="reclassify",
            reclassify_to="not_a_bug",
        )
    )
    reopened = h.apply(h.event(EventType.ISSUE_REOPENED, issue_id=issue.id, body="updated"))
    assert reopened.issue is not None and reopened.issue.revision == 2
    with expect(ErrorCode.STALE_RESULT):
        h.apply(
            h.event(
                EventType.REPRODUCTION_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                revision=1,
                session_id=str(old_session),
                reproduced=True,
            )
        )
    h.classify(h.issue(issue.id))
    with expect(ErrorCode.STALE_RESULT):
        h.apply(
            h.event(
                EventType.REPRODUCTION_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                session_id=str(old_session),
                reproduced=True,
            )
        )
    assert h.issue(issue.id).state == IssueState.REPRODUCING


def test_version_conflict_is_explicit(h: Harness) -> None:
    issue = h.open_issue()
    with expect(ErrorCode.VERSION_CONFLICT):
        h.apply(
            h.event(EventType.CLASSIFICATION_RESULT, issue_id=issue.id, role=ActorRole.AGENT),
            expected_version=issue.version + 5,
        )


def test_rejected_event_is_recorded_and_redelivery_is_duplicate(h: Harness) -> None:
    issue = h.open_issue()
    event = h.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id, delivery_id="late-1")
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(event)
    again = h.apply(h.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id, delivery_id="late-1"))
    assert again.duplicate is True
    assert again.attempt.status == AttemptStatus.REJECTED
    assert again.attempt.error_code == ErrorCode.HUMAN_GATE_REQUIRED.value


def test_reminders_then_inactivity_close_and_reopen(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    policy = h.issue(issue.id).reporter_wait
    assert policy is not None and policy.policy_revision == 1 and policy.reminders_sent == 0
    reminded = h.timer(issue, EventType.REMINDER_ELAPSED)
    assert reminded.attempt.transition == TransitionName.REMIND_REPORTER
    policy = h.issue(issue.id).reporter_wait
    assert policy is not None and policy.reminders_sent == 1
    assert len(h.jobs(issue.id, JobKind.PUBLISH_REMINDER)) == 1
    # second reminder exhausts the budget: no third timer is scheduled
    h.timer(issue, EventType.REMINDER_ELAPSED)
    policy = h.issue(issue.id).reporter_wait
    assert policy is not None and policy.reminders_sent == 2 and policy.reminder_due_at is None
    assert len(h.jobs(issue.id, JobKind.PUBLISH_REMINDER)) == 2
    assert len(h.jobs(issue.id, JobKind.REMINDER)) == 2
    closed = h.timer(issue, EventType.INACTIVITY_ELAPSED)
    assert closed.issue is not None and closed.issue.state == IssueState.CLOSED_INACTIVE
    reopened = h.apply(h.event(EventType.ISSUE_REOPENED, issue_id=issue.id))
    assert reopened.issue is not None and reopened.issue.state == IssueState.TRIAGE


def test_early_and_stale_timers_cannot_act(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    with expect(ErrorCode.INVALID_INPUT):
        h.timer(issue, EventType.REMINDER_ELAPSED, advance=False)
    with expect(ErrorCode.STALE_RESULT):
        h.timer(issue, EventType.REMINDER_ELAPSED, policy_revision=7)
    with expect(ErrorCode.STALE_RESULT):
        h.timer(issue, EventType.INACTIVITY_ELAPSED, due_at="2020-01-01T00:00:00+00:00")
    assert h.jobs(issue.id, JobKind.PUBLISH_REMINDER) == []
    assert h.issue(issue.id).state == IssueState.AWAITING_REPORTER


def test_reminder_does_not_loop_and_stops_once_answered(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    h.timer(issue, EventType.REMINDER_ELAPSED)
    before = h.jobs(issue.id, JobKind.REMINDER)
    # replaying the same timer payload is rejected as stale (due time moved)
    policy = h.issue(issue.id).reporter_wait
    assert policy is not None
    with expect(ErrorCode.STALE_RESULT):
        h.timer(issue, EventType.REMINDER_ELAPSED, due_at=policy.started_at.isoformat())
    assert h.jobs(issue.id, JobKind.REMINDER) == before
    h.apply(
        h.event(
            EventType.REPORTER_RESPONSE,
            issue_id=issue.id,
            role=ActorRole.REPORTER,
            login="reporter-1",
            answers=[{"field": "logs", "answer": "attached"}],
        )
    )
    assert h.issue(issue.id).state == IssueState.REPRODUCING
    skipped = h.timer(issue, EventType.INACTIVITY_ELAPSED)
    assert skipped.attempt.status == AttemptStatus.NOOP


def test_duplicate_and_reclassify_paths(h: Harness) -> None:
    first = h.open_issue(number=1)
    dup = h.apply(h.event(EventType.DUPLICATE_DETECTED, issue_id=first.id, duplicate_of=99))
    assert dup.issue is not None and dup.issue.state == IssueState.DUPLICATE
    second = h.open_issue(number=2)
    reclass = h.apply(
        h.event(
            EventType.OWNER_DECISION,
            issue_id=second.id,
            role=ActorRole.OWNER,
            decision="reclassify",
            reclassify_to="not_a_bug",
        )
    )
    assert reclass.issue is not None and reclass.issue.state == IssueState.NOT_A_BUG


def test_environment_block_then_operator_retry(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    blocked = h.apply(
        h.event(
            EventType.ENVIRONMENT_BLOCKED,
            issue_id=issue.id,
            role=ActorRole.AGENT,
            session_id=str(h.latest_session_id(issue.id)),
            reason="docker pull failed",
        )
    )
    assert blocked.issue is not None and blocked.issue.state == IssueState.BLOCKED_ENVIRONMENT
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.AGENT))
    retried = h.apply(
        h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.OPERATOR)
    )
    assert retried.issue is not None and retried.issue.state == IssueState.REPRODUCING


def _live_sessions(h: Harness, issue_id: uuid.UUID) -> list[SessionState]:
    with h.uow() as uow:
        return [s.state for s in uow.list_sessions(issue_id=issue_id) if s.workspace_live]


def test_automation_error_retry_starts_new_reproduction_session(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    failed = h.apply(h.event(EventType.AUTOMATION_FAILURE, issue_id=issue.id, reason="bug"))
    assert failed.issue is not None and failed.issue.state == IssueState.AUTOMATION_ERROR
    assert _live_sessions(h, issue.id) == []
    retried = h.apply(
        h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.OPERATOR)
    )
    assert retried.issue is not None and retried.issue.state == IssueState.REPRODUCING
    assert _live_sessions(h, issue.id) == [SessionState.RUNNING]
    assert len(h.jobs(issue.id, JobKind.START_REPRODUCTION)) == 2


def test_automation_error_during_fix_requeues_bounded_fix_not_fixing(h: Harness) -> None:
    issue = h.start_fix(h.confirm(h.reproduce(h.classify(h.open_issue()))))
    assert issue.state == IssueState.FIXING
    h.apply(h.event(EventType.AUTOMATION_FAILURE, issue_id=issue.id, reason="crash"))
    assert _live_sessions(h, issue.id) == []
    retried = h.apply(
        h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.OPERATOR)
    )
    assert retried.issue is not None
    assert retried.issue.state == IssueState.FIX_AUTHORIZED
    assert retried.issue.state not in ACTIVE_AGENT_STATES
    assert _live_sessions(h, issue.id) == []
    with h.uow() as uow:
        queued = [s for s in uow.list_sessions(issue_id=issue.id) if s.state == SessionState.QUEUED]
    assert len(queued) == 1
    assert any(":retry:" in key for key in h.jobs(issue.id, JobKind.START_FIX))
    resumed = h.start_fix(retried.issue)
    assert resumed.state == IssueState.FIXING
    assert _live_sessions(h, issue.id) == [SessionState.RUNNING]


def test_automation_error_with_expired_authorization_returns_to_owner_gate(h: Harness) -> None:
    issue = h.start_fix(h.confirm(h.reproduce(h.classify(h.open_issue()))))
    h.apply(h.event(EventType.AUTOMATION_FAILURE, issue_id=issue.id, reason="crash"))
    h.clock.advance(days=8)
    retried = h.apply(
        h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.OPERATOR)
    )
    assert retried.issue is not None
    assert retried.issue.state == IssueState.NEEDS_OWNER_DECISION
    assert _live_sessions(h, issue.id) == []


def test_session_message_and_cancel_are_jobs_not_transitions(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    session_id = h.latest_session_id(issue.id)
    msg = h.apply(
        h.event(
            EventType.SESSION_MESSAGE,
            issue_id=issue.id,
            role=ActorRole.OPERATOR,
            session_id=str(session_id),
            body="Focus on the CSV path.",
        )
    )
    assert msg.attempt.status == AttemptStatus.NOOP
    cancel = h.apply(
        h.event(
            EventType.SESSION_CANCEL_REQUESTED,
            issue_id=issue.id,
            role=ActorRole.OPERATOR,
            session_id=str(session_id),
        )
    )
    assert cancel.attempt.status == AttemptStatus.NOOP
    with h.uow() as uow:
        kinds = {j.kind for j in uow.list_jobs(issue.id)}
        session = uow.get_session(session_id)
        messages = uow.list_messages(session_id)
    assert {JobKind.DELIVER_SESSION_MESSAGE, JobKind.CANCEL_SESSION} <= kinds
    assert session is not None and session.cancel_requested is True
    assert len(messages) == 1 and messages[0].delivered is False
    assert h.issue(issue.id).version == issue.version
