from __future__ import annotations

from types import TracebackType

import pytest
from app.domain.errors import DomainError, ErrorCode
from app.domain.models import AttemptStatus, JobKind, QuestionStatus
from app.domain.states import (
    TARGET_REPOSITORY,
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    ActorRole,
    EventType,
    IssueState,
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


def test_answered_evidence_starts_reproduction_with_live_workspace(h: Harness) -> None:
    issue = h.classify(h.open_issue(), MISSING)
    result = h.apply(
        h.event(
            EventType.REPORTER_RESPONSE,
            issue_id=issue.id,
            role=ActorRole.REPORTER,
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
    public_pending = [
        j for j in jobs if j.status.value == "pending" and j.kind != JobKind.PRIVATE_SECURITY_TASK
    ]
    assert public_pending == [] or all(j.kind == JobKind.CLASSIFY for j in public_pending)


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
    issue = h.to_pr_open()
    result = h.apply(
        h.event(
            EventType.DEVIN_REVIEW_COMPLETED,
            issue_id=issue.id,
            role=ActorRole.AGENT,
            verdict="passed",
            findings=0,
        )
    )
    assert result.issue is not None and result.issue.state == IssueState.AWAITING_OWNER
    with h.uow() as uow:
        pr = uow.list_pull_requests(issue.id)[-1]
    assert pr.devin_review.value == "passed"
    assert pr.human_approved is False
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(h.event(EventType.PR_MERGED, issue_id=issue.id))
    with expect(ErrorCode.HUMAN_GATE_REQUIRED):
        h.apply(
            h.event(
                EventType.HUMAN_REVIEW_SUBMITTED,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                state="approved",
            )
        )


def test_human_approval_then_merge_completes(h: Harness) -> None:
    issue = h.to_pr_open()
    approved = h.apply(
        h.event(
            EventType.HUMAN_REVIEW_SUBMITTED,
            issue_id=issue.id,
            role=ActorRole.OWNER,
            login="owner-1",
            state="approved",
        )
    )
    assert approved.issue is not None and approved.issue.state == IssueState.AWAITING_OWNER
    merged = h.apply(h.event(EventType.PR_MERGED, issue_id=issue.id))
    assert merged.issue is not None and merged.issue.state == IssueState.COMPLETED
    assert merged.attempt.transition == TransitionName.COMPLETE


def test_changes_requested_resumes_fix_without_new_confirmation(h: Harness) -> None:
    issue = h.to_pr_open()
    changed = h.apply(
        h.event(
            EventType.OWNER_DECISION,
            issue_id=issue.id,
            role=ActorRole.OWNER,
            decision="request_changes",
            rationale="tighten test",
        )
    )
    assert changed.issue is not None and changed.issue.state == IssueState.CHANGES_REQUESTED
    resumed = h.start_fix(changed.issue)
    assert resumed.state == IssueState.FIXING


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
    reminded = h.apply(h.event(EventType.REMINDER_ELAPSED, issue_id=issue.id))
    assert reminded.attempt.transition == TransitionName.REMIND_REPORTER
    closed = h.apply(h.event(EventType.INACTIVITY_ELAPSED, issue_id=issue.id))
    assert closed.issue is not None and closed.issue.state == IssueState.CLOSED_INACTIVE
    reopened = h.apply(h.event(EventType.ISSUE_REOPENED, issue_id=issue.id))
    assert reopened.issue is not None and reopened.issue.state == IssueState.TRIAGE


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


def test_automation_error_retry_restores_previous_state(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    failed = h.apply(h.event(EventType.AUTOMATION_FAILURE, issue_id=issue.id, reason="bug"))
    assert failed.issue is not None and failed.issue.state == IssueState.AUTOMATION_ERROR
    retried = h.apply(
        h.event(EventType.RETRY_REQUESTED, issue_id=issue.id, role=ActorRole.OPERATOR)
    )
    assert retried.issue is not None and retried.issue.state == IssueState.REPRODUCING


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
