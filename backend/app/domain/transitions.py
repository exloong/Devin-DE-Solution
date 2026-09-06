"""Deterministic transition service.

Every inbound event is applied inside one unit of work:

1. deduplicate by ``(source, delivery_id)``;
2. lock the issue (per-issue serialization);
3. reject stale revisions and optimistic-version mismatches;
4. run the typed handler for the event, which selects a transition from the
   legal table and performs side effects (questions, jobs, sessions, evidence);
5. validate source state, actor role, and destination against the table;
6. bump the resource version, record the attempt and audit entry;
7. release any agent workspace when the issue enters a waiting state.

Rejected events are still stored with a rejected attempt so that redelivery of
the same event is a no-op.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.errors import DomainError, ErrorCode
from app.domain.models import (
    PRIVATE_JOB_KINDS,
    PUBLIC_JOB_KINDS,
    AgentSession,
    AttemptStatus,
    AuditEntry,
    ConversationAuthor,
    ConversationMessage,
    Event,
    Evidence,
    EvidenceKind,
    HumanDecision,
    InformationRequest,
    Issue,
    IssueRevision,
    Job,
    JobKind,
    JobStatus,
    PullRequestState,
    QuestionStatus,
    ReporterWaitPolicy,
    Repository,
    RepositoryScope,
    ReviewerRouting,
    ReviewVerdict,
    RoutingCandidate,
    RoutingState,
    SessionBudget,
    SessionEvent,
    SessionOutput,
    TransitionAttempt,
    utcnow,
)
from app.domain.ports import Clock, UnitOfWork
from app.domain.states import (
    ACTIVE_AGENT_STATES,
    SESSION_TERMINAL,
    TRANSITIONS,
    ActorRole,
    DecisionKind,
    EventType,
    IssueState,
    SessionKind,
    SessionState,
    TransitionName,
    TransitionSpec,
)


class SystemClock:
    def now(self) -> datetime:
        return utcnow()


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _parse_timestamp(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError(ErrorCode.INVALID_INPUT, "due_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise DomainError(ErrorCode.INVALID_INPUT, "due_at must be timezone-aware")
    return parsed


SECURITY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bsecurity\b",
        r"\bvulnerab",
        r"\bCVE-\d{4}-\d+",
        r"\bXSS\b",
        r"\bSQL injection\b",
        r"\bRCE\b",
        r"\bprivilege escalation\b",
        r"\bexploit\b",
        r"\bauth(entication|orization)? bypass\b",
    )
)
SECURITY_LABELS: frozenset[str] = frozenset({"security", "vulnerability"})


def detect_security_signal(title: str, body: str, labels: list[str]) -> bool:
    if any(label.lower() in SECURITY_LABELS for label in labels):
        return True
    text = f"{title}\n{body}"
    return any(p.search(text) for p in SECURITY_PATTERNS)


@dataclass(frozen=True)
class TransitionResult:
    event: Event
    attempt: TransitionAttempt
    issue: Issue | None
    duplicate: bool = False

    @property
    def applied(self) -> bool:
        return self.attempt.status == AttemptStatus.APPLIED


JOB_RETRY_BACKOFF_BASE = timedelta(seconds=30)
JOB_RETRY_BACKOFF_MAX = timedelta(minutes=30)

# States in which a queued (not yet started, no workspace) FIX session may wait.
_HOLDS_QUEUED_FIX: frozenset[IssueState] = frozenset(
    {IssueState.FIX_AUTHORIZED, IssueState.CHANGES_REQUESTED}
)


@dataclass
class _Outcome:
    """What a handler decided. `transition is None` means a no-op attempt."""

    transition: TransitionName | None
    to_state: IssueState | None
    detail: str = ""


Handler = Callable[["_Context"], _Outcome]


@dataclass
class _Context:
    uow: UnitOfWork
    event: Event
    issue: Issue
    now: datetime


class TransitionService:
    def __init__(
        self,
        scope: RepositoryScope | None = None,
        clock: Clock | None = None,
        reminder_after: timedelta = timedelta(days=3),
        inactivity_after: timedelta = timedelta(days=14),
        fix_authorization_ttl: timedelta = timedelta(days=7),
    ) -> None:
        self.scope = scope or RepositoryScope()
        self.clock = clock or SystemClock()
        self.reminder_after = reminder_after
        self.inactivity_after = inactivity_after
        self.fix_authorization_ttl = fix_authorization_ttl
        self._handlers: dict[EventType, Handler] = {
            EventType.ISSUE_OPENED: self._on_issue_opened,
            EventType.ISSUE_REOPENED: self._on_issue_reopened,
            EventType.CLASSIFICATION_RESULT: self._on_classification_result,
            EventType.REPORTER_RESPONSE: self._on_reporter_response,
            EventType.REPORTER_COMMENT: self._on_reporter_response,
            EventType.REPRODUCTION_STARTED: self._on_reproduction_started,
            EventType.SESSION_PROGRESS: self._on_session_progress,
            EventType.REPRODUCTION_RESULT: self._on_reproduction_result,
            EventType.ENVIRONMENT_BLOCKED: self._on_environment_blocked,
            EventType.OWNER_DECISION: self._on_owner_decision,
            EventType.HUMAN_REVIEW_SUBMITTED: self._on_human_review,
            EventType.FIX_SESSION_STARTED: self._on_fix_session_started,
            EventType.FIX_RESULT: self._on_fix_result,
            EventType.DEVIN_REVIEW_COMPLETED: self._on_devin_review,
            EventType.PR_OPENED: self._on_pr_opened_webhook,
            EventType.PR_SYNCHRONIZED: self._on_pr_synchronized,
            EventType.PR_MERGED: self._on_pr_merged,
            EventType.REVIEWER_ROUTING_RESOLVED: self._on_reviewer_routing_resolved,
            EventType.REMINDER_ELAPSED: self._on_reminder,
            EventType.INACTIVITY_ELAPSED: self._on_inactivity,
            EventType.DUPLICATE_DETECTED: self._on_duplicate,
            EventType.SECURITY_SIGNAL: self._on_security_signal,
            EventType.AUTOMATION_FAILURE: self._on_automation_failure,
            EventType.RETRY_REQUESTED: self._on_retry,
            EventType.SESSION_CANCEL_REQUESTED: self._on_session_cancel,
            EventType.SESSION_MESSAGE: self._on_session_message,
        }

    # ------------------------------------------------------------------ apply

    def apply(
        self,
        uow: UnitOfWork,
        event: Event,
        *,
        expected_version: int | None = None,
        expected_session_version: int | None = None,
    ) -> TransitionResult:
        existing = uow.get_event_by_delivery(event.source, event.delivery_id)
        if existing is not None:
            attempt = uow.get_attempt_for_event(existing.id)
            if attempt is None:
                raise DomainError(ErrorCode.INTERNAL, "event stored without attempt")
            issue = uow.get_issue(existing.issue_id) if existing.issue_id else None
            return TransitionResult(existing, attempt, issue, duplicate=True)

        now = self.clock.now()
        try:
            if event.type == EventType.ISSUE_OPENED:
                issue = self._create_issue(uow, event, now)
            else:
                if event.issue_id is None:
                    raise DomainError(ErrorCode.INVALID_INPUT, "event requires issue_id")
                found = uow.get_issue(event.issue_id)
                if found is None:
                    raise DomainError(ErrorCode.NOT_FOUND, "issue not found")
                issue = found
            uow.lock_issue(issue.id)
            event.issue_id = issue.id
            event.correlation_id = issue.correlation_id

            if expected_version is not None and expected_version != issue.version:
                raise DomainError(
                    ErrorCode.VERSION_CONFLICT,
                    "resource version does not match",
                    {"expected": expected_version, "actual": issue.version},
                )
            if expected_session_version is not None:
                self._check_session_version(uow, event, issue, expected_session_version)
            if event.issue_revision is not None and event.issue_revision != issue.revision:
                raise DomainError(
                    ErrorCode.STALE_RESULT,
                    "event targets a superseded issue revision",
                    {"event_revision": event.issue_revision, "current": issue.revision},
                )

            handler = self._handlers.get(event.type)
            if handler is None:
                raise DomainError(ErrorCode.INVALID_INPUT, f"unsupported event {event.type.value}")

            ctx = _Context(uow=uow, event=event, issue=issue, now=now)
            from_state = issue.state
            outcome = handler(ctx)
            attempt = self._commit_outcome(ctx, from_state, outcome)
            uow.add_event(event)
            uow.add_attempt(attempt)
            uow.commit()
            return TransitionResult(event, attempt, issue)
        except DomainError as err:
            uow.rollback()
            rejected_issue = uow.get_issue(event.issue_id) if event.issue_id else None
            attempt = TransitionAttempt(
                issue_id=event.issue_id or uuid.UUID(int=0),
                event_id=event.id,
                issue_revision=rejected_issue.revision if rejected_issue else 0,
                transition=None,
                from_state=rejected_issue.state if rejected_issue else IssueState.NEW,
                to_state=None,
                status=AttemptStatus.REJECTED,
                error_code=err.code.value,
                detail=err.message,
                resource_version=rejected_issue.version if rejected_issue else 0,
                correlation_id=event.correlation_id,
                created_at=now,
            )
            if rejected_issue is not None:
                uow.add_event(event)
                uow.add_attempt(attempt)
                uow.add_audit(
                    AuditEntry(
                        actor=event.actor,
                        action=f"rejected:{event.type.value}",
                        resource_type="issue",
                        resource_id=str(rejected_issue.id),
                        resource_version=rejected_issue.version,
                        correlation_id=event.correlation_id,
                        detail=f"{err.code.value}: {err.message}",
                        created_at=now,
                    )
                )
                uow.commit()
            raise

    def _check_session_version(
        self, uow: UnitOfWork, event: Event, issue: Issue, expected: int
    ) -> None:
        raw = event.payload.get("session_id")
        if not isinstance(raw, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "session precondition without session_id")
        session = uow.get_session(uuid.UUID(raw))
        if session is None or session.issue_id != issue.id:
            raise DomainError(ErrorCode.NOT_FOUND, "session not found", {"session_id": raw})
        if expected != session.version:
            raise DomainError(
                ErrorCode.VERSION_CONFLICT,
                "session version does not match",
                {"expected": expected, "actual": session.version, "resource": "session"},
            )

    def _commit_outcome(
        self, ctx: _Context, from_state: IssueState, outcome: _Outcome
    ) -> TransitionAttempt:
        issue = ctx.issue
        if outcome.transition is None:
            status = AttemptStatus.NOOP
            to_state: IssueState | None = None
        else:
            spec = TRANSITIONS[outcome.transition]
            if outcome.to_state is None:
                raise DomainError(ErrorCode.INTERNAL, "transition without destination")
            self._validate(spec, ctx.event, from_state, outcome.to_state)
            issue.previous_state = from_state
            issue.state = outcome.to_state
            issue.version += 1
            issue.updated_at = ctx.now
            ctx.uow.save_issue(issue)
            status = AttemptStatus.APPLIED
            to_state = outcome.to_state
            self._enforce_workspace_release(ctx)

        attempt = TransitionAttempt(
            issue_id=issue.id,
            event_id=ctx.event.id,
            issue_revision=issue.revision,
            transition=outcome.transition,
            from_state=from_state,
            to_state=to_state,
            status=status,
            detail=outcome.detail,
            resource_version=issue.version,
            correlation_id=issue.correlation_id,
            created_at=ctx.now,
        )
        ctx.uow.add_audit(
            AuditEntry(
                actor=ctx.event.actor,
                action=f"{status.value}:{ctx.event.type.value}",
                resource_type="issue",
                resource_id=str(issue.id),
                resource_version=issue.version,
                correlation_id=issue.correlation_id,
                detail=outcome.detail,
                created_at=ctx.now,
            )
        )
        return attempt

    @staticmethod
    def _validate(
        spec: TransitionSpec, event: Event, from_state: IssueState, to_state: IssueState
    ) -> None:
        if event.type not in spec.events:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                f"{event.type.value} cannot trigger {spec.name.value}",
            )
        if from_state not in spec.sources:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                f"{spec.name.value} is not legal from {from_state.value}",
                {"transition": spec.name.value, "from": from_state.value},
            )
        if to_state not in spec.destinations:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                f"{spec.name.value} cannot reach {to_state.value}",
            )
        if event.actor.role not in spec.actors:
            code = (
                ErrorCode.HUMAN_GATE_REQUIRED if spec.human_gate else ErrorCode.UNAUTHORIZED_ACTOR
            )
            raise DomainError(
                code,
                f"{event.actor.role.value} may not perform {spec.name.value}",
                {"allowed": sorted(a.value for a in spec.actors)},
            )

    def _enforce_workspace_release(self, ctx: _Context) -> None:
        if ctx.issue.state in ACTIVE_AGENT_STATES:
            return
        for session in ctx.uow.list_sessions(issue_id=ctx.issue.id):
            if session.state in SESSION_TERMINAL and session.workspace_released:
                continue
            if session.state == SessionState.RUNNING:
                session.state = SessionState.COMPLETED
                session.finished_at = ctx.now
            if session.state == SessionState.QUEUED and ctx.issue.state not in _HOLDS_QUEUED_FIX:
                session.state = SessionState.CANCELLED
                session.finished_at = ctx.now
            if not session.workspace_released:
                session.workspace_released_at = ctx.now
            session.workspace_released = True
            session.workspace_name = None
            session.current_action = None
            session.next_checkpoint = None
            session.updated_at = ctx.now
            self._save_session(ctx, session)

    # --------------------------------------------------------------- helpers

    def _save_session(self, ctx: _Context, session: AgentSession) -> None:
        session.version += 1
        ctx.uow.save_session(session)

    def _enqueue(
        self,
        ctx: _Context,
        kind: JobKind,
        *,
        suffix: str = "",
        payload: dict[str, object] | None = None,
        run_after: datetime | None = None,
    ) -> Job | None:
        if ctx.issue.security_flagged and kind in PUBLIC_JOB_KINDS:
            raise DomainError(
                ErrorCode.SECURITY_FAIL_CLOSED,
                "security-flagged issues cannot generate public side effects",
                {"job": kind.value},
            )
        key = f"{ctx.issue.id}:{kind.value}:r{ctx.issue.revision}:{suffix}"
        if ctx.uow.get_job_by_key(key) is not None:
            return None
        job = Job(
            issue_id=ctx.issue.id,
            kind=kind,
            idempotency_key=key,
            payload=payload or {},
            run_after=run_after or ctx.now,
            correlation_id=ctx.issue.correlation_id,
            issue_revision=ctx.issue.revision,
            created_at=ctx.now,
        )
        ctx.uow.add_job(job)
        return job

    def _cancel_pending_jobs(self, ctx: _Context, public_only: bool) -> None:
        """Cancel outstanding work for the issue.

        ``public_only`` keeps private-security work alive; every pending *or
        claimed* job of any other kind is cancelled so a worker that already
        picked one up sees the cancellation when it rechecks before acting.
        """
        for job in ctx.uow.list_jobs(ctx.issue.id):
            if job.status not in (JobStatus.PENDING, JobStatus.CLAIMED):
                continue
            if public_only and job.kind in PRIVATE_JOB_KINDS:
                continue
            job.status = JobStatus.CANCELLED
            job.finished_at = ctx.now
            ctx.uow.save_job(job)

    # ------------------------------------------------------------------- jobs

    def _job_may_run(self, uow: UnitOfWork, job: Job) -> None:
        """Recheck a job against current issue reality; raise instead of acting.

        A worker must call this via :meth:`claim_job` before starting and via
        :meth:`run_claimed_job` around the side effect, so a job that was
        claimed before a security routing or a revision bump cannot act.
        """
        if job.status == JobStatus.CANCELLED:
            raise DomainError(
                ErrorCode.PROHIBITED_ACTION, "job was cancelled", {"job_id": str(job.id)}
            )
        if job.status in (JobStatus.DONE, JobStatus.FAILED):
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "job already finished",
                {"job_id": str(job.id), "status": job.status.value},
            )
        if job.issue_id is None:
            return
        issue = uow.get_issue(job.issue_id)
        if issue is None:
            raise DomainError(ErrorCode.NOT_FOUND, "issue not found")
        if issue.security_flagged and job.kind not in PRIVATE_JOB_KINDS:
            job.status = JobStatus.CANCELLED
            job.finished_at = self.clock.now()
            uow.save_job(job)
            uow.commit()
            raise DomainError(
                ErrorCode.SECURITY_FAIL_CLOSED,
                "issue is security-private; non-private job must not run",
                {"job_id": str(job.id), "kind": job.kind.value},
            )
        if job.issue_revision is not None and job.issue_revision != issue.revision:
            job.status = JobStatus.CANCELLED
            job.finished_at = self.clock.now()
            uow.save_job(job)
            uow.commit()
            raise DomainError(
                ErrorCode.STALE_RESULT,
                "job targets a superseded issue revision",
                {"job_revision": job.issue_revision, "current": issue.revision},
            )

    def _load_job(self, uow: UnitOfWork, job_id: uuid.UUID) -> Job:
        job = uow.get_job(job_id)
        if job is None:
            raise DomainError(ErrorCode.NOT_FOUND, "job not found", {"job_id": str(job_id)})
        if job.issue_id is not None:
            uow.lock_issue(job.issue_id)
        return job

    def claim_job(self, uow: UnitOfWork, job_id: uuid.UUID, worker: str) -> Job:
        """Move a pending job to CLAIMED after rechecking it is still allowed."""
        job = self._load_job(uow, job_id)
        now = self.clock.now()
        if job.status == JobStatus.CLAIMED:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "job already claimed",
                {"job_id": str(job.id), "claimed_by": job.claimed_by},
            )
        self._job_may_run(uow, job)
        if job.run_after > now:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "job is not due yet",
                {"run_after": job.run_after.isoformat()},
            )
        job.status = JobStatus.CLAIMED
        job.claimed_by = worker
        job.claimed_at = now
        job.attempts += 1
        uow.save_job(job)
        uow.commit()
        return job

    @contextmanager
    def run_claimed_job(self, uow: UnitOfWork, job_id: uuid.UUID, worker: str) -> Iterator[Job]:
        """Authorize a claimed job and hold the issue lock through its side effect.

        Usage::

            with service.run_claimed_job(uow, job.id, "worker-a") as job:
                external_side_effect(job)

        The job is rechecked (cancelled / security-private / stale revision) while
        the per-issue lock is held, so a security routing cannot slip in between
        the check and the effect. The job becomes DONE only after the block exits
        normally. If the block raises, the job is scheduled for a bounded retry
        (or FAILED once ``max_attempts`` is exhausted) and the error re-raised, so a
        failed effect is never recorded as done.
        """
        job = self._load_job(uow, job_id)
        if job.status != JobStatus.CLAIMED or job.claimed_by != worker:
            self._job_may_run(uow, job)
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "job is not claimed by this worker",
                {"job_id": str(job.id), "status": job.status.value, "claimed_by": job.claimed_by},
            )
        self._job_may_run(uow, job)
        try:
            yield job
        except Exception:
            uow.rollback()
            self._schedule_retry(uow, job)
            raise
        job.status = JobStatus.DONE
        job.finished_at = self.clock.now()
        uow.save_job(job)
        uow.commit()

    def _schedule_retry(self, uow: UnitOfWork, job: Job) -> None:
        now = self.clock.now()
        job.claimed_by = None
        job.claimed_at = None
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.finished_at = now
        else:
            job.status = JobStatus.PENDING
            job.run_after = now + min(
                JOB_RETRY_BACKOFF_MAX, JOB_RETRY_BACKOFF_BASE * (2 ** (job.attempts - 1))
            )
        uow.save_job(job)
        uow.commit()

    def _payload_str(self, ctx: _Context, key: str, default: str | None = None) -> str:
        value = ctx.event.payload.get(key, default)
        if not isinstance(value, str):
            raise DomainError(ErrorCode.INVALID_INPUT, f"payload.{key} must be a string")
        return value

    def _payload_int(self, ctx: _Context, key: str, default: int | None = None) -> int:
        value = ctx.event.payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise DomainError(ErrorCode.INVALID_INPUT, f"payload.{key} must be an integer")
        return value

    def _payload_list(self, ctx: _Context, key: str) -> list[object]:
        value = ctx.event.payload.get(key, [])
        if not isinstance(value, list):
            raise DomainError(ErrorCode.INVALID_INPUT, f"payload.{key} must be a list")
        return value

    def _payload_session(self, ctx: _Context) -> AgentSession:
        raw = ctx.event.payload.get("session_id")
        if not isinstance(raw, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.session_id is required")
        try:
            session_id = uuid.UUID(raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.session_id is not a UUID") from exc
        session = ctx.uow.get_session(session_id)
        if session is None or session.issue_id != ctx.issue.id:
            raise DomainError(ErrorCode.NOT_FOUND, "session not found for issue")
        if session.issue_revision != ctx.issue.revision:
            raise DomainError(
                ErrorCode.STALE_RESULT,
                "session belongs to a superseded issue revision",
                {"session_revision": session.issue_revision, "current": ctx.issue.revision},
            )
        return session

    def _open_required_questions(self, ctx: _Context) -> list[InformationRequest]:
        return [
            q
            for q in ctx.uow.list_questions(ctx.issue.id, ctx.issue.revision)
            if q.required and q.status != QuestionStatus.ANSWERED
        ]

    def _assert_reproduction_ready(self, ctx: _Context) -> None:
        if ctx.issue.security_flagged:
            raise DomainError(
                ErrorCode.SECURITY_FAIL_CLOSED,
                "security-flagged issues are not reproduced publicly",
            )
        open_questions = self._open_required_questions(ctx)
        if open_questions:
            raise DomainError(
                ErrorCode.MISSING_EVIDENCE,
                "required reporter evidence is missing or unavailable",
                {
                    "fields": [q.field for q in open_questions],
                    "statuses": [q.status.value for q in open_questions],
                },
            )

    def _create_session(
        self,
        ctx: _Context,
        kind: SessionKind,
        title: str,
        trigger: str,
        budget: SessionBudget,
        *,
        target_commit: str | None,
    ) -> AgentSession:
        for existing in ctx.uow.list_sessions(issue_id=ctx.issue.id):
            if (
                existing.kind == kind
                and existing.issue_revision == ctx.issue.revision
                and existing.state not in SESSION_TERMINAL
            ):
                return existing
        session = AgentSession(
            issue_id=ctx.issue.id,
            issue_revision=ctx.issue.revision,
            kind=kind,
            title=title,
            trigger=trigger,
            budget=budget,
            target_commit=target_commit,
            repository=self.scope.full_name,
            dry_run=self.scope.dry_run,
            correlation_id=ctx.issue.correlation_id,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.uow.add_session(session)
        ctx.uow.add_session_event(
            SessionEvent(
                session_id=session.id,
                label="Session queued",
                detail=f"Bounded {kind.value} session queued for {self.scope.full_name}.",
                created_at=ctx.now,
            )
        )
        return session

    def _latest_revision(self, ctx: _Context) -> IssueRevision | None:
        revisions = ctx.uow.list_revisions(ctx.issue.id)
        return revisions[-1] if revisions else None

    def _finish_session(
        self, ctx: _Context, session: AgentSession, state: SessionState, label: str, detail: str
    ) -> None:
        session.state = state
        if not session.workspace_released:
            session.workspace_released_at = ctx.now
        session.workspace_released = True
        session.workspace_name = None
        session.finished_at = ctx.now
        session.updated_at = ctx.now
        session.current_action = None
        session.next_checkpoint = None
        self._save_session(ctx, session)
        ctx.uow.add_session_event(
            SessionEvent(session_id=session.id, label=label, detail=detail, created_at=ctx.now)
        )

    # -------------------------------------------------------------- handlers

    def _create_issue(self, uow: UnitOfWork, event: Event, now: datetime) -> Issue:
        payload = event.payload
        repository_name = payload.get("repository")
        if not isinstance(repository_name, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.repository is required")
        self.scope.assert_allowed(repository_name)
        repo = uow.get_repository()
        if repo is None:
            repo = Repository(
                full_name=self.scope.full_name,
                default_branch=self.scope.default_branch,
                dry_run=self.scope.dry_run,
                created_at=now,
            )
            uow.add_repository(repo)
        number = payload.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.number must be an integer")
        existing = uow.get_issue_by_number(repo.id, number)
        if existing is not None:
            event.issue_id = existing.id
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "issue already exists; reopen creates a new revision",
                {"issue_id": str(existing.id)},
            )
        title = payload.get("title", "")
        reporter = payload.get("reporter", "unknown")
        category = payload.get("category", "Uncategorized")
        owner_team = payload.get("owner_team")
        if not isinstance(title, str) or not isinstance(reporter, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "title and reporter must be strings")
        issue = Issue(
            repository_id=repo.id,
            external_number=number,
            title=title,
            reporter_login=reporter,
            category=category if isinstance(category, str) else "Uncategorized",
            owner_team=owner_team if isinstance(owner_team, str) else None,
            correlation_id=event.correlation_id,
            created_at=now,
            updated_at=now,
        )
        uow.add_issue(issue)
        return issue

    def _on_issue_opened(self, ctx: _Context) -> _Outcome:
        body = ctx.event.payload.get("body", "")
        labels_raw = self._payload_list(ctx, "labels")
        labels = [label for label in labels_raw if isinstance(label, str)]
        if not isinstance(body, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.body must be a string")
        target_commit = ctx.event.payload.get("target_commit")
        superset_version = ctx.event.payload.get("superset_version")
        ctx.uow.add_revision(
            IssueRevision(
                issue_id=ctx.issue.id,
                revision=1,
                title=ctx.issue.title,
                body=body,
                labels=labels,
                target_commit=target_commit if isinstance(target_commit, str) else None,
                superset_version=superset_version if isinstance(superset_version, str) else None,
                created_at=ctx.now,
            )
        )
        if detect_security_signal(ctx.issue.title, body, labels):
            ctx.issue.security_flagged = True
            self._enqueue(ctx, JobKind.PRIVATE_SECURITY_TASK)
            return _Outcome(
                TransitionName.INTAKE,
                IssueState.SECURITY_PRIVATE,
                "security signal detected; public processing stopped",
            )
        self._enqueue(ctx, JobKind.CLASSIFY)
        return _Outcome(TransitionName.INTAKE, IssueState.TRIAGE, "issue normalized for triage")

    def _on_issue_reopened(self, ctx: _Context) -> _Outcome:
        previous = self._latest_revision(ctx)
        body = ctx.event.payload.get("body", previous.body if previous else "")
        ctx.issue.revision += 1
        ctx.uow.add_revision(
            IssueRevision(
                issue_id=ctx.issue.id,
                revision=ctx.issue.revision,
                title=ctx.issue.title,
                body=body if isinstance(body, str) else "",
                labels=list(previous.labels) if previous else [],
                target_commit=previous.target_commit if previous else None,
                superset_version=previous.superset_version if previous else None,
                created_at=ctx.now,
            )
        )
        self._enqueue(ctx, JobKind.CLASSIFY)
        return _Outcome(TransitionName.REOPEN, IssueState.TRIAGE, f"revision {ctx.issue.revision}")

    def _on_classification_result(self, ctx: _Context) -> _Outcome:
        payload = ctx.event.payload
        if payload.get("security_signal") is True:
            return self._route_security(ctx, "agent classification raised a security signal")
        duplicate_of = payload.get("duplicate_of")
        if isinstance(duplicate_of, int) and not isinstance(duplicate_of, bool):
            return _Outcome(
                TransitionName.MARK_DUPLICATE,
                IssueState.DUPLICATE,
                f"duplicate of #{duplicate_of}",
            )
        category = payload.get("category")
        if isinstance(category, str):
            ctx.issue.category = category
        if category == "not_a_bug":
            return _Outcome(
                TransitionName.CLASSIFY_TERMINAL,
                IssueState.NOT_A_BUG,
                "agent classified the report as not a bug",
            )
        if category == "unsupported":
            return _Outcome(
                TransitionName.CLASSIFY_TERMINAL,
                IssueState.UNSUPPORTED,
                "agent classified the report as unsupported",
            )
        if category == "duplicate":
            return _Outcome(
                TransitionName.MARK_DUPLICATE,
                IssueState.DUPLICATE,
                "agent classified the report as a duplicate",
            )
        owner_team = payload.get("owner_team")
        if isinstance(owner_team, str):
            ctx.issue.owner_team = owner_team
        missing = self._payload_list(ctx, "missing_fields")
        created = 0
        for item in missing:
            if not isinstance(item, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "missing_fields entries must be objects")
            field = item.get("field")
            prompt = item.get("prompt")
            if not isinstance(field, str) or not isinstance(prompt, str):
                raise DomainError(ErrorCode.INVALID_INPUT, "missing_fields need field and prompt")
            if any(
                q.field == field
                for q in ctx.uow.list_questions(ctx.issue.id, ctx.issue.revision)
                if q.status != QuestionStatus.SUPERSEDED
            ):
                continue
            ctx.uow.add_question(
                InformationRequest(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    field=field,
                    prompt=prompt,
                    why_it_matters=str(item.get("why_it_matters", "")),
                    safe_example=str(item.get("safe_example", "")),
                    required=bool(item.get("required", True)),
                    reminder_due_at=ctx.now + self.reminder_after,
                    inactivity_due_at=ctx.now + self.inactivity_after,
                    created_at=ctx.now,
                )
            )
            created += 1
        if self._open_required_questions(ctx):
            self._enqueue(ctx, JobKind.PUBLISH_QUESTIONS, suffix=str(ctx.issue.version))
            self._begin_reporter_wait(ctx)
            return _Outcome(
                TransitionName.REQUEST_INFORMATION,
                IssueState.AWAITING_REPORTER,
                f"{created} focused question(s) requested",
            )
        return self._start_reproduction(ctx, "classification found reproduction context complete")

    def _on_reporter_response(self, ctx: _Context) -> _Outcome:
        self._authorize_reporter_response(ctx)
        answers = self._payload_list(ctx, "answers")
        if not answers:
            raise DomainError(ErrorCode.INVALID_INPUT, "at least one answer is required")
        questions = {
            q.field: q
            for q in ctx.uow.list_questions(ctx.issue.id, ctx.issue.revision)
            if q.status != QuestionStatus.SUPERSEDED
        }
        unavailable: list[str] = []
        for raw in answers:
            if not isinstance(raw, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "answers entries must be objects")
            field = raw.get("field")
            if not isinstance(field, str) or field not in questions:
                raise DomainError(
                    ErrorCode.INVALID_INPUT,
                    "answer does not match an open question",
                    {"field": field},
                )
            question = questions[field]
            if raw.get("unavailable") is True:
                question.status = QuestionStatus.UNAVAILABLE
                question.answer = None
                unavailable.append(field)
            else:
                answer = raw.get("answer")
                if not isinstance(answer, str) or not answer.strip():
                    question.status = QuestionStatus.INVALID
                else:
                    question.status = QuestionStatus.ANSWERED
                    question.answer = answer
            question.answered_at = ctx.now
            ctx.uow.save_question(question)
        ctx.uow.add_evidence(
            Evidence(
                issue_id=ctx.issue.id,
                issue_revision=ctx.issue.revision,
                kind=EvidenceKind.REPORTER_SNAPSHOT,
                title="Reporter response snapshot",
                summary=f"{len(answers)} answer(s) recorded; content stored as data only",
                created_at=ctx.now,
            )
        )
        if unavailable:
            self._enqueue(
                ctx,
                JobKind.SAFE_ALTERNATIVE_REVIEW,
                suffix=",".join(sorted(unavailable)),
                payload={"fields": unavailable},
            )
        if not self._open_required_questions(ctx):
            self._cancel_pending_jobs(ctx, public_only=False)
            return self._start_reproduction(ctx, "reporter completed the reproduction contract")
        return _Outcome(
            TransitionName.RECORD_REPORTER_RESPONSE,
            IssueState.AWAITING_REPORTER,
            "answers recorded; required context still missing",
        )

    def _authorize_reporter_response(self, ctx: _Context) -> None:
        """Only the original reporter may answer; an operator needs an audited override."""
        actor = ctx.event.actor
        if actor.role == ActorRole.REPORTER:
            if actor.login != ctx.issue.reporter_login:
                raise DomainError(
                    ErrorCode.UNAUTHORIZED_ACTOR,
                    "only the original reporter may answer reproduction questions",
                    {"reporter": ctx.issue.reporter_login},
                )
            return
        if actor.role == ActorRole.OPERATOR:
            rationale = ctx.event.payload.get("override_rationale")
            if not isinstance(rationale, str) or not rationale.strip():
                raise DomainError(
                    ErrorCode.UNAUTHORIZED_ACTOR,
                    "operators may record reporter answers only with an override_rationale",
                )
            ctx.uow.add_audit(
                AuditEntry(
                    actor=actor,
                    action="override:reporter_response",
                    resource_type="issue",
                    resource_id=str(ctx.issue.id),
                    resource_version=ctx.issue.version,
                    correlation_id=ctx.event.correlation_id,
                    detail=rationale.strip(),
                    created_at=ctx.now,
                )
            )
            return
        raise DomainError(
            ErrorCode.UNAUTHORIZED_ACTOR,
            f"{actor.role.value} may not record reporter answers",
        )

    def _start_reproduction(self, ctx: _Context, trigger: str) -> _Outcome:
        self._assert_reproduction_ready(ctx)
        revision = self._latest_revision(ctx)
        session = self._create_session(
            ctx,
            SessionKind.REPRODUCTION,
            f"Reproduce {ctx.issue.key}",
            trigger,
            SessionBudget(
                wall_clock_seconds=3600,
                allowed_capabilities=["read_repo", "run_isolated_tests"],
            ),
            target_commit=revision.target_commit if revision else None,
        )
        session.state = SessionState.RUNNING
        session.workspace_released = False
        session.workspace_name = f"superset-repro-{ctx.issue.external_number}"
        session.started_at = ctx.now
        session.last_heartbeat_at = ctx.now
        session.updated_at = ctx.now
        self._save_session(ctx, session)
        self._enqueue(
            ctx,
            JobKind.START_REPRODUCTION,
            suffix=str(session.id),
            payload={"session_id": str(session.id)},
        )
        return _Outcome(TransitionName.START_REPRODUCTION, IssueState.REPRODUCING, trigger)

    def _on_reproduction_started(self, ctx: _Context) -> _Outcome:
        return self._start_reproduction(ctx, "reproduction requested")

    def _on_session_progress(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        if ctx.issue.state not in ACTIVE_AGENT_STATES or session.state != SessionState.RUNNING:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "no live workspace is permitted for this issue state",
                {"issue_state": ctx.issue.state.value, "session_state": session.state.value},
            )
        payload = ctx.event.payload
        synced = False
        if "progress_percent" in payload:
            session.progress_percent = max(0, min(100, self._payload_int(ctx, "progress_percent")))
            synced = True
        if "current_action" in payload:
            session.current_action = self._payload_str(ctx, "current_action")
            synced = True
        if "next_checkpoint" in payload:
            session.next_checkpoint = self._payload_str(ctx, "next_checkpoint")
            synced = True
        if synced:
            session.progress_source = f"{ctx.event.source}:{ctx.event.actor.login}"
            session.progress_synced_at = ctx.now
        session.last_heartbeat_at = ctx.now
        session.updated_at = ctx.now
        self._save_session(ctx, session)
        label = ctx.event.payload.get("label")
        if isinstance(label, str):
            ctx.uow.add_session_event(
                SessionEvent(
                    session_id=session.id,
                    label=label,
                    detail=session.current_action or "",
                    created_at=ctx.now,
                )
            )
        message = ctx.event.payload.get("message")
        if isinstance(message, str):
            ctx.uow.add_message(
                ConversationMessage(
                    session_id=session.id,
                    author=ConversationAuthor.DEVIN,
                    author_login="devin",
                    body=message,
                    delivered=True,
                    created_at=ctx.now,
                )
            )
        return _Outcome(None, None, "heartbeat recorded")

    def _record_output(
        self,
        ctx: _Context,
        session: AgentSession,
        schema_name: str,
        accepted: bool,
        reason: str | None,
    ) -> None:
        ctx.uow.add_output(
            SessionOutput(
                session_id=session.id,
                schema_name=schema_name,
                accepted=accepted,
                rejection_reason=reason,
                issue_revision=ctx.issue.revision,
                payload={k: v for k, v in ctx.event.payload.items() if k != "session_id"},
                created_at=ctx.now,
            )
        )

    def _on_reproduction_result(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        if session.kind != SessionKind.REPRODUCTION:
            raise DomainError(ErrorCode.INVALID_INPUT, "session is not a reproduction session")
        reproduced = ctx.event.payload.get("reproduced")
        if not isinstance(reproduced, bool):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.reproduced must be a boolean")
        for item in self._payload_list(ctx, "evidence"):
            if not isinstance(item, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "evidence entries must be objects")
            kind_raw = item.get("kind")
            title = item.get("title")
            if not isinstance(kind_raw, str) or not isinstance(title, str):
                raise DomainError(ErrorCode.INVALID_INPUT, "evidence needs kind and title")
            try:
                kind = EvidenceKind(kind_raw)
            except ValueError as exc:
                raise DomainError(
                    ErrorCode.INVALID_INPUT, f"unknown evidence kind {kind_raw}"
                ) from exc
            ctx.uow.add_evidence(
                Evidence(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    session_id=session.id,
                    kind=kind,
                    title=title,
                    summary=str(item.get("summary", "")),
                    created_at=ctx.now,
                )
            )
        self._record_output(ctx, session, "reproduction_result.v1", True, None)
        summary = "reproduced" if reproduced else "not reproduced"
        self._finish_session(
            ctx,
            session,
            SessionState.COMPLETED,
            "Evidence packet published",
            f"Workspace released; owner packet ready ({summary}).",
        )
        self._enqueue(ctx, JobKind.ESCALATE_OWNER, run_after=ctx.now + timedelta(days=2))
        return _Outcome(
            TransitionName.REPRODUCTION_COMPLETED,
            IssueState.NEEDS_OWNER_DECISION,
            f"reproduction {summary}; awaiting owner decision",
        )

    def _on_environment_blocked(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        reason = self._payload_str(ctx, "reason", "environment failure")
        self._record_output(ctx, session, "reproduction_result.v1", False, reason)
        self._finish_session(
            ctx, session, SessionState.NEEDS_ATTENTION, "Environment blocked", reason
        )
        self._enqueue(ctx, JobKind.RECOVERY, suffix=str(ctx.issue.version))
        return _Outcome(TransitionName.ENVIRONMENT_BLOCKED, IssueState.BLOCKED_ENVIRONMENT, reason)

    def _route_security(self, ctx: _Context, detail: str) -> _Outcome:
        ctx.issue.security_flagged = True
        self._cancel_pending_jobs(ctx, public_only=True)
        for session in ctx.uow.list_sessions(issue_id=ctx.issue.id):
            if session.state not in SESSION_TERMINAL:
                self._finish_session(
                    ctx, session, SessionState.CANCELLED, "Cancelled", "security fail-closed"
                )
        self._enqueue(ctx, JobKind.PRIVATE_SECURITY_TASK)
        return _Outcome(TransitionName.ROUTE_SECURITY_PRIVATE, IssueState.SECURITY_PRIVATE, detail)

    def _on_security_signal(self, ctx: _Context) -> _Outcome:
        return self._route_security(ctx, self._payload_str(ctx, "reason", "security signal"))

    def _on_owner_decision(self, ctx: _Context) -> _Outcome:
        kind_raw = self._payload_str(ctx, "decision")
        try:
            kind = DecisionKind(kind_raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_INPUT, f"unknown decision {kind_raw}") from exc
        allowed_roles = {ActorRole.OWNER}
        if kind == DecisionKind.ROUTE_SECURITY_PRIVATE:
            allowed_roles |= {ActorRole.SECURITY, ActorRole.OPERATOR}
        if kind in (DecisionKind.APPROVE_PR, DecisionKind.REQUEST_CHANGES):
            allowed_roles |= {ActorRole.OPERATOR}
        if ctx.event.actor.role not in allowed_roles:
            raise DomainError(
                ErrorCode.HUMAN_GATE_REQUIRED,
                f"{kind.value} requires a human owner decision",
                {"actor": ctx.event.actor.role.value},
            )
        rationale = self._payload_str(ctx, "rationale", "")
        decision = HumanDecision(
            issue_id=ctx.issue.id,
            issue_revision=ctx.issue.revision,
            kind=kind,
            actor=ctx.event.actor,
            rationale=rationale,
            created_at=ctx.now,
        )
        if kind == DecisionKind.CONFIRM_BUG:
            if ctx.issue.state != IssueState.NEEDS_OWNER_DECISION:
                raise DomainError(
                    ErrorCode.ILLEGAL_TRANSITION,
                    "confirm_bug requires an evidence packet awaiting owner decision",
                    {"from": ctx.issue.state.value},
                )
            decision.scope = self._payload_str(
                ctx, "scope", "regression test plus minimal fix in exloong/superset"
            )
            decision.expires_at = ctx.now + self.fix_authorization_ttl
            ctx.uow.add_decision(decision)
            self._queue_fix_session(
                ctx, decision, f"{ctx.event.actor.login} confirmed expected behavior"
            )
            return _Outcome(TransitionName.CONFIRM_BUG, IssueState.FIX_AUTHORIZED, rationale)
        if kind == DecisionKind.REQUEST_DISCRIMINATOR:
            field = self._payload_str(ctx, "field")
            prompt = self._payload_str(ctx, "prompt")
            ctx.uow.add_decision(decision)
            ctx.uow.add_question(
                InformationRequest(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    field=field,
                    prompt=prompt,
                    why_it_matters=rationale,
                    reminder_due_at=ctx.now + self.reminder_after,
                    inactivity_due_at=ctx.now + self.inactivity_after,
                    created_at=ctx.now,
                )
            )
            self._enqueue(ctx, JobKind.PUBLISH_QUESTIONS, suffix=f"discriminator:{field}")
            self._begin_reporter_wait(ctx)
            return _Outcome(
                TransitionName.REQUEST_DISCRIMINATOR, IssueState.AWAITING_REPORTER, field
            )
        if kind == DecisionKind.RECLASSIFY:
            target_raw = self._payload_str(ctx, "reclassify_to")
            try:
                target = IssueState(target_raw)
            except ValueError as exc:
                raise DomainError(ErrorCode.INVALID_INPUT, "invalid reclassify_to") from exc
            decision.reclassify_to = target
            ctx.uow.add_decision(decision)
            self._cancel_pending_jobs(ctx, public_only=False)
            return _Outcome(TransitionName.RECLASSIFY, target, rationale)
        if kind == DecisionKind.ROUTE_SECURITY_PRIVATE:
            ctx.uow.add_decision(decision)
            return self._route_security(ctx, rationale or "owner routed to security")
        pr = self._pr_ref(ctx)
        override = self._authorize_review_actor(ctx, pr, rationale)
        if kind == DecisionKind.APPROVE_PR:
            ctx.uow.add_decision(decision)
            self._mark_pr_approval(ctx, pr, True, ctx.event.actor.login, override)
            return _Outcome(
                TransitionName.OWNER_APPROVED,
                IssueState.AWAITING_OWNER,
                f"human approval recorded for #{pr.number}@{pr.head_sha}; merge stays on GitHub",
            )
        ctx.uow.add_decision(decision)
        self._mark_pr_approval(ctx, pr, False, None, override)
        self._requeue_fix_after_changes(ctx, ctx.event.actor.login)
        return _Outcome(
            TransitionName.OWNER_REQUESTED_CHANGES, IssueState.CHANGES_REQUESTED, rationale
        )

    # ------------------------------------------------- pull request binding

    def _tracked_pr(self, ctx: _Context, number: int) -> PullRequestState:
        for pr in ctx.uow.list_pull_requests(ctx.issue.id):
            if pr.number == number:
                return pr
        raise DomainError(
            ErrorCode.INVALID_INPUT,
            "pull request is not tracked for this issue",
            {"pr_number": number},
        )

    def _pr_ref(self, ctx: _Context) -> PullRequestState:
        """Resolve ``payload.pr_number`` + ``payload.head_sha`` to the tracked PR.

        Every review/approval/merge signal must name the exact head it refers to;
        a signal for a superseded head is stale and cannot advance state.
        """
        number = self._payload_int(ctx, "pr_number")
        head_sha = self._payload_str(ctx, "head_sha")
        pr = self._tracked_pr(ctx, number)
        if head_sha != pr.head_sha:
            raise DomainError(
                ErrorCode.STALE_RESULT,
                "signal refers to a superseded pull request head",
                {"pr_number": number, "signal_head": head_sha, "tracked_head": pr.head_sha},
            )
        return pr

    def _routing_for_head(self, ctx: _Context, pr: PullRequestState) -> ReviewerRouting | None:
        for routing in reversed(list(ctx.uow.list_reviewer_routings(ctx.issue.id))):
            if routing.pull_request_number == pr.number and routing.head_sha == pr.head_sha:
                return routing
        return None

    def _authorize_review_actor(self, ctx: _Context, pr: PullRequestState, rationale: str) -> bool:
        """Return True when the decision is an explicit operator override."""
        actor = ctx.event.actor
        if actor.role == ActorRole.OPERATOR:
            if ctx.event.payload.get("override") is not True or not rationale.strip():
                raise DomainError(
                    ErrorCode.HUMAN_GATE_REQUIRED,
                    "operators may only approve via an explicit override with a rationale",
                )
            return True
        if actor.role != ActorRole.OWNER:
            raise DomainError(ErrorCode.HUMAN_GATE_REQUIRED, "reviews must come from a human owner")
        routing = self._routing_for_head(ctx, pr)
        if routing is None:
            raise DomainError(
                ErrorCode.UNAUTHORIZED_ACTOR,
                "no reviewer routing is persisted for this pull request head",
                {"pr_number": pr.number, "head_sha": pr.head_sha},
            )
        if routing.state != RoutingState.RESOLVED or actor.login not in routing.authorized_logins():
            raise DomainError(
                ErrorCode.UNAUTHORIZED_ACTOR,
                "reviewer is not an authorized owner for this pull request head",
                {
                    "login": actor.login,
                    "routing_state": routing.state.value,
                    "authorized": sorted(routing.authorized_logins()),
                },
            )
        return False

    def _mark_pr_approval(
        self,
        ctx: _Context,
        pr: PullRequestState,
        approved: bool,
        approver: str | None,
        override: bool,
    ) -> None:
        pr.human_approved = approved
        pr.human_approver = approver
        pr.approved_head_sha = pr.head_sha if approved else None
        pr.approval_override = override if approved else False
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)

    def _reset_pr_head(self, ctx: _Context, pr: PullRequestState, head_sha: str) -> None:
        """A new head invalidates approval and review evidence bound to the old head."""
        previous = pr.head_sha
        pr.head_sha = head_sha
        pr.human_approved = False
        pr.human_approver = None
        pr.approved_head_sha = None
        pr.approval_override = False
        pr.devin_review = ReviewVerdict.PENDING
        pr.devin_review_findings = 0
        pr.devin_review_head_sha = None
        pr.devin_review_url = None
        pr.routing_id = None
        pr.checks_passed = None
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)
        ctx.uow.add_evidence(
            Evidence(
                issue_id=ctx.issue.id,
                issue_revision=ctx.issue.revision,
                session_id=pr.session_id,
                kind=EvidenceKind.REVIEW_FINDINGS,
                title=f"PR #{pr.number} head changed",
                summary=f"{previous} -> {head_sha}; prior approval and review evidence invalidated",
                created_at=ctx.now,
            )
        )
        self._request_review_and_routing(ctx, pr)

    def _request_review_and_routing(self, ctx: _Context, pr: PullRequestState) -> None:
        self._enqueue(
            ctx,
            JobKind.TRIGGER_DEVIN_REVIEW,
            suffix=pr.head_sha,
            payload={"pr_number": pr.number, "head_sha": pr.head_sha},
        )
        self._enqueue(
            ctx,
            JobKind.RESOLVE_REVIEWERS,
            suffix=pr.head_sha,
            payload={"pr_number": pr.number, "head_sha": pr.head_sha},
        )

    def _on_human_review(self, ctx: _Context) -> _Outcome:
        state = self._payload_str(ctx, "state")
        if state not in ("approved", "changes_requested"):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "review state must be approved or changes_requested"
            )
        pr = self._pr_ref(ctx)
        rationale = self._payload_str(ctx, "rationale", "")
        override = self._authorize_review_actor(ctx, pr, rationale)
        reviewer = ctx.event.actor.login
        if state == "approved":
            ctx.uow.add_decision(
                HumanDecision(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    kind=DecisionKind.APPROVE_PR,
                    actor=ctx.event.actor,
                    rationale=rationale,
                    created_at=ctx.now,
                )
            )
            self._mark_pr_approval(ctx, pr, True, reviewer, override)
            return _Outcome(
                TransitionName.OWNER_APPROVED,
                IssueState.AWAITING_OWNER,
                f"{reviewer} approved #{pr.number}@{pr.head_sha}",
            )
        ctx.uow.add_decision(
            HumanDecision(
                issue_id=ctx.issue.id,
                issue_revision=ctx.issue.revision,
                kind=DecisionKind.REQUEST_CHANGES,
                actor=ctx.event.actor,
                rationale=rationale,
                created_at=ctx.now,
            )
        )
        self._mark_pr_approval(ctx, pr, False, None, override)
        self._requeue_fix_after_changes(ctx, reviewer)
        return _Outcome(
            TransitionName.OWNER_REQUESTED_CHANGES, IssueState.CHANGES_REQUESTED, reviewer
        )

    def _requeue_fix_after_changes(self, ctx: _Context, reviewer: str) -> None:
        """Requested changes re-queue one bounded fix session under the live authorization.

        If the confirm_bug authorization has expired nothing is queued; a later
        FIX_SESSION_STARTED is then rejected with human_gate_required.
        """
        try:
            authorization = self._active_fix_authorization(ctx)
        except DomainError:
            return
        self._queue_fix_session(
            ctx,
            authorization,
            f"{reviewer} requested changes on #{self._pr_ref(ctx).number}",
            job_suffix=f"changes:{ctx.issue.version}",
        )

    def _queue_fix_session(
        self, ctx: _Context, authorization: HumanDecision, trigger: str, *, job_suffix: str = ""
    ) -> AgentSession:
        """Queue the single bounded FIX session for the current revision + START_FIX job."""
        revision = self._latest_revision(ctx)
        session = self._create_session(
            ctx,
            SessionKind.FIX,
            f"Fix {ctx.issue.key}",
            trigger,
            SessionBudget(
                wall_clock_seconds=5400,
                allowed_capabilities=["read_repo", "run_isolated_tests", "open_pull_request"],
            ),
            target_commit=revision.target_commit if revision else None,
        )
        self._enqueue(
            ctx,
            JobKind.START_FIX,
            suffix=job_suffix,
            payload={"session_id": str(session.id), "authorization_id": str(authorization.id)},
        )
        return session

    def _active_fix_authorization(self, ctx: _Context) -> HumanDecision:
        for decision in reversed(list(ctx.uow.list_decisions(ctx.issue.id))):
            if (
                decision.kind == DecisionKind.CONFIRM_BUG
                and decision.issue_revision == ctx.issue.revision
                and decision.superseded_by is None
                and (decision.expires_at is None or decision.expires_at > ctx.now)
            ):
                return decision
        raise DomainError(
            ErrorCode.HUMAN_GATE_REQUIRED,
            "an unexpired confirm_bug decision is required before coding",
        )

    def _queued_fix_session(self, ctx: _Context) -> AgentSession:
        """Return the single queued FIX session authorized for the current revision.

        The session is created once by ``confirm_bug`` (or a retry under the
        same authorization); starting it never creates another one.
        """
        if "session_id" in ctx.event.payload:
            session = self._payload_session(ctx)
        else:
            queued = [
                s
                for s in ctx.uow.list_sessions(issue_id=ctx.issue.id)
                if s.kind == SessionKind.FIX
                and s.issue_revision == ctx.issue.revision
                and s.state == SessionState.QUEUED
            ]
            if not queued:
                raise DomainError(
                    ErrorCode.ILLEGAL_TRANSITION,
                    "no queued fix session exists for this issue revision",
                )
            if len(queued) > 1:
                raise DomainError(
                    ErrorCode.INTERNAL, "multiple queued fix sessions for one revision"
                )
            session = queued[0]
        if session.kind != SessionKind.FIX:
            raise DomainError(ErrorCode.INVALID_INPUT, "session is not a fix session")
        if session.state != SessionState.QUEUED:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "fix session is not queued",
                {"session_id": str(session.id), "state": session.state.value},
            )
        return session

    def _on_fix_session_started(self, ctx: _Context) -> _Outcome:
        authorization = self._active_fix_authorization(ctx)
        session = self._queued_fix_session(ctx)
        session.state = SessionState.RUNNING
        session.workspace_released = False
        session.workspace_name = f"superset-fix-{ctx.issue.external_number}"
        session.branch = f"devin/{ctx.issue.external_number}-fix"
        session.started_at = ctx.now
        session.last_heartbeat_at = ctx.now
        session.updated_at = ctx.now
        self._save_session(ctx, session)
        ctx.uow.add_session_event(
            SessionEvent(
                session_id=session.id,
                label="Fix authorized",
                detail=f"Scope: {authorization.scope}",
                created_at=ctx.now,
            )
        )
        transition = (
            TransitionName.RESUME_FIX
            if ctx.issue.state == IssueState.CHANGES_REQUESTED
            else TransitionName.START_FIX
        )
        return _Outcome(transition, IssueState.FIXING, "bounded fix session started")

    def _on_fix_result(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        if session.kind != SessionKind.FIX:
            raise DomainError(ErrorCode.INVALID_INPUT, "session is not a fix session")
        raw_pr = ctx.event.payload.get("pull_request")
        if not isinstance(raw_pr, dict):
            raise DomainError(ErrorCode.INVALID_INPUT, "payload.pull_request is required")
        repository = raw_pr.get("repository", self.scope.full_name)
        if not isinstance(repository, str):
            raise DomainError(ErrorCode.INVALID_INPUT, "pull_request.repository must be a string")
        self.scope.assert_allowed(repository)
        number = raw_pr.get("number")
        head_branch = raw_pr.get("head_branch")
        head_sha = raw_pr.get("head_sha")
        url = raw_pr.get("url")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or not isinstance(head_branch, str)
            or not isinstance(head_sha, str)
            or not isinstance(url, str)
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "pull_request fields are incomplete")
        expected_url = self.scope.pull_request_url(number)
        if url != expected_url:
            raise DomainError(
                ErrorCode.REPOSITORY_NOT_ALLOWED,
                "pull request url must point at the scoped repository",
                {"expected": expected_url},
            )
        forbidden = {"reviewer_candidates", "reviewers", "reviewer_routing_rule", "owners"}
        if forbidden & set(raw_pr):
            raise DomainError(
                ErrorCode.PROHIBITED_ACTION,
                "agents cannot choose reviewers; routing is a trusted CODEOWNERS adapter outcome",
                {"fields": sorted(forbidden & set(raw_pr))},
            )
        existing_pr = next(
            (p for p in ctx.uow.list_pull_requests(ctx.issue.id) if p.number == number), None
        )
        pr_title = raw_pr.get("title")
        if existing_pr is not None:
            pr = existing_pr
            pr.session_id = session.id
            pr.head_branch = head_branch
            if isinstance(pr_title, str):
                pr.title = pr_title
            if pr.head_sha != head_sha:
                self._reset_pr_head(ctx, pr, head_sha)
            else:
                ctx.uow.save_pull_request(pr)
        else:
            pr = PullRequestState(
                issue_id=ctx.issue.id,
                session_id=session.id,
                repository=repository,
                number=number,
                title=pr_title if isinstance(pr_title, str) else None,
                head_branch=head_branch,
                head_sha=head_sha,
                url=url,
                created_at=ctx.now,
                updated_at=ctx.now,
            )
            ctx.uow.add_pull_request(pr)
            self._request_review_and_routing(ctx, pr)
        for kind, title in (
            (EvidenceKind.REGRESSION_TEST, "Regression test"),
            (EvidenceKind.PATCH, "PR diff"),
            (EvidenceKind.TEST_OUTPUT, "Test output"),
        ):
            ctx.uow.add_evidence(
                Evidence(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    session_id=session.id,
                    kind=kind,
                    title=title,
                    created_at=ctx.now,
                )
            )
        self._record_output(ctx, session, "fix_result.v1", True, None)
        session.branch = head_branch
        self._finish_session(
            ctx,
            session,
            SessionState.COMPLETED,
            "Draft PR opened",
            f"Linked PR #{number} to the issue and released the workspace.",
        )
        return _Outcome(
            TransitionName.OPEN_PR, IssueState.PR_OPEN, f"PR #{number} opened at {head_sha}"
        )

    def _on_reviewer_routing_resolved(self, ctx: _Context) -> _Outcome:
        if ctx.event.actor.role != ActorRole.SYSTEM:
            raise DomainError(
                ErrorCode.UNAUTHORIZED_ACTOR,
                "reviewer routing is produced only by the trusted CODEOWNERS adapter",
                {"actor": ctx.event.actor.role.value},
            )
        pr = self._pr_ref(ctx)
        state_raw = self._payload_str(ctx, "state")
        try:
            state = RoutingState(state_raw)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.INVALID_INPUT, f"unknown routing state {state_raw}"
            ) from exc
        candidates: list[RoutingCandidate] = []
        for raw in self._payload_list(ctx, "candidates"):
            if not isinstance(raw, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "candidates entries must be objects")
            team = raw.get("team")
            rule = raw.get("rule")
            members = raw.get("members", [])
            paths = raw.get("paths", [])
            if not isinstance(team, str) or not isinstance(rule, str):
                raise DomainError(ErrorCode.INVALID_INPUT, "candidates need team and rule")
            candidates.append(
                RoutingCandidate(
                    team=team,
                    rule=rule,
                    members=_str_list(members),
                    paths=_str_list(paths),
                    rationale=str(raw.get("rationale", "")),
                    selected=raw.get("selected", True) is True,
                )
            )
        routing = ReviewerRouting(
            issue_id=ctx.issue.id,
            pull_request_number=pr.number,
            head_sha=pr.head_sha,
            state=state,
            candidates=candidates,
            unowned_paths=_str_list(self._payload_list(ctx, "unowned_paths")),
            ambiguous_paths=_str_list(self._payload_list(ctx, "ambiguous_paths")),
            rationale=self._payload_str(ctx, "rationale", ""),
            resolved_by=ctx.event.actor,
            created_at=ctx.now,
        )
        if state == RoutingState.RESOLVED and not routing.authorized_logins():
            raise DomainError(
                ErrorCode.INVALID_INPUT, "resolved routing must name at least one owner login"
            )
        ctx.uow.add_reviewer_routing(routing)
        pr.routing_id = routing.id
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)
        if state == RoutingState.RESOLVED:
            self._enqueue(
                ctx,
                JobKind.REQUEST_REVIEWERS,
                suffix=pr.head_sha,
                payload={
                    "pr_number": pr.number,
                    "head_sha": pr.head_sha,
                    "teams": [c.team for c in candidates if c.selected],
                    "reviewers": sorted(routing.authorized_logins()),
                    "routing_id": str(routing.id),
                },
            )
            return _Outcome(None, None, f"reviewer routing resolved for #{pr.number}@{pr.head_sha}")
        self._enqueue(
            ctx,
            JobKind.ESCALATE_OWNER,
            suffix=f"routing:{pr.head_sha}",
            payload={
                "pr_number": pr.number,
                "head_sha": pr.head_sha,
                "routing_state": state.value,
                "unowned_paths": routing.unowned_paths,
                "ambiguous_paths": routing.ambiguous_paths,
            },
        )
        return _Outcome(None, None, f"reviewer routing {state.value}; operator escalation queued")

    def _on_pr_synchronized(self, ctx: _Context) -> _Outcome:
        number = self._payload_int(ctx, "pr_number")
        head_sha = self._payload_str(ctx, "head_sha")
        pr = self._tracked_pr(ctx, number)
        if pr.head_sha == head_sha:
            return _Outcome(None, None, "head unchanged")
        if ctx.issue.state == IssueState.FIXING:
            return _Outcome(None, None, "fix session in progress; head is bound on fix_result")
        self._reset_pr_head(ctx, pr, head_sha)
        return _Outcome(
            TransitionName.PR_HEAD_UPDATED,
            IssueState.PR_OPEN,
            f"PR #{number} head moved to {head_sha}; approval and review invalidated",
        )

    def _on_pr_opened_webhook(self, ctx: _Context) -> _Outcome:
        prs = ctx.uow.list_pull_requests(ctx.issue.id)
        number = self._payload_int(ctx, "number")
        if not any(pr.number == number for pr in prs):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "pull request was not created by a Relay fix session"
            )
        return _Outcome(None, None, f"PR #{number} webhook acknowledged")

    def _on_devin_review(self, ctx: _Context) -> _Outcome:
        pr = self._pr_ref(ctx)
        verdict_raw = self._payload_str(ctx, "verdict")
        try:
            pr.devin_review = ReviewVerdict(verdict_raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_INPUT, "invalid review verdict") from exc
        pr.devin_review_findings = self._payload_int(ctx, "findings", 0)
        pr.devin_review_head_sha = pr.head_sha
        url = ctx.event.payload.get("url")
        pr.devin_review_url = url if isinstance(url, str) else None
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)
        ctx.uow.add_evidence(
            Evidence(
                issue_id=ctx.issue.id,
                issue_revision=ctx.issue.revision,
                session_id=pr.session_id,
                kind=EvidenceKind.REVIEW_FINDINGS,
                title=f"Devin Review ({pr.devin_review.value})",
                summary=f"{pr.devin_review_findings} finding(s); not a merge approval",
                created_at=ctx.now,
            )
        )
        return _Outcome(
            TransitionName.DEVIN_REVIEW_RECORDED,
            IssueState.AWAITING_OWNER,
            "Devin Review attached; awaiting human owner",
        )

    def _on_pr_merged(self, ctx: _Context) -> _Outcome:
        pr = self._pr_ref(ctx)
        if not pr.approval_binds(pr.head_sha):
            raise DomainError(
                ErrorCode.HUMAN_GATE_REQUIRED,
                "merged head has no bound human approval; Devin Review is not approval",
                {
                    "pr_number": pr.number,
                    "head_sha": pr.head_sha,
                    "approved_head_sha": pr.approved_head_sha,
                },
            )
        pr.merged = True
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)
        self._cancel_pending_jobs(ctx, public_only=False)
        return _Outcome(TransitionName.COMPLETE, IssueState.COMPLETED, f"PR #{pr.number} merged")

    # ---------------------------------------------------------------- timers

    def _begin_reporter_wait(self, ctx: _Context) -> None:
        previous = ctx.issue.reporter_wait
        policy = ReporterWaitPolicy(
            policy_revision=(previous.policy_revision + 1) if previous else 1,
            started_at=ctx.now,
            reminder_due_at=ctx.now + self.reminder_after,
            inactivity_due_at=ctx.now + self.inactivity_after,
        )
        ctx.issue.reporter_wait = policy
        self._schedule_timer(ctx, JobKind.REMINDER, policy, policy.reminder_due_at, 1)
        self._schedule_timer(ctx, JobKind.INACTIVITY, policy, policy.inactivity_due_at, 0)

    def _schedule_timer(
        self,
        ctx: _Context,
        kind: JobKind,
        policy: ReporterWaitPolicy,
        due_at: datetime | None,
        sequence: int,
    ) -> None:
        if due_at is None:
            return
        self._enqueue(
            ctx,
            kind,
            suffix=f"p{policy.policy_revision}:n{sequence}",
            payload={
                "policy_revision": policy.policy_revision,
                "due_at": due_at.isoformat(),
                "sequence": sequence,
            },
            run_after=due_at,
        )

    def _validate_timer(self, ctx: _Context, kind: JobKind) -> ReporterWaitPolicy:
        policy = ctx.issue.reporter_wait
        if policy is None:
            raise DomainError(ErrorCode.STALE_RESULT, "issue is not waiting on the reporter")
        revision = self._payload_int(ctx, "policy_revision")
        if revision != policy.policy_revision:
            raise DomainError(
                ErrorCode.STALE_RESULT,
                "timer belongs to a superseded reporter-wait policy",
                {"timer_revision": revision, "current": policy.policy_revision},
            )
        due_at = _parse_timestamp(self._payload_str(ctx, "due_at"))
        expected = policy.reminder_due_at if kind == JobKind.REMINDER else policy.inactivity_due_at
        if expected is None or due_at != expected:
            raise DomainError(
                ErrorCode.STALE_RESULT,
                "timer due time does not match the scheduled timer",
                {
                    "due_at": due_at.isoformat(),
                    "scheduled": expected.isoformat() if expected else None,
                },
            )
        if ctx.now < due_at:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                "timer fired before its due time",
                {"now": ctx.now.isoformat(), "due_at": due_at.isoformat()},
            )
        return policy

    def _on_reminder(self, ctx: _Context) -> _Outcome:
        policy = self._validate_timer(ctx, JobKind.REMINDER)
        open_questions = self._open_required_questions(ctx)
        if not open_questions:
            return _Outcome(None, None, "no open questions; reminder skipped")
        policy.reminders_sent += 1
        self._enqueue(
            ctx,
            JobKind.PUBLISH_REMINDER,
            suffix=f"p{policy.policy_revision}:n{policy.reminders_sent}",
            payload={
                "reminder_number": policy.reminders_sent,
                "fields": [q.field for q in open_questions],
            },
        )
        if policy.reminders_sent < policy.max_reminders:
            policy.reminder_due_at = ctx.now + self.reminder_after
            self._schedule_timer(
                ctx, JobKind.REMINDER, policy, policy.reminder_due_at, policy.reminders_sent + 1
            )
        else:
            policy.reminder_due_at = None
        return _Outcome(
            TransitionName.REMIND_REPORTER,
            IssueState.AWAITING_REPORTER,
            f"reminder {policy.reminders_sent}/{policy.max_reminders} queued for publication",
        )

    def _on_inactivity(self, ctx: _Context) -> _Outcome:
        policy = self._validate_timer(ctx, JobKind.INACTIVITY)
        if not self._open_required_questions(ctx):
            return _Outcome(None, None, "no open questions; inactivity skipped")
        policy.reminder_due_at = None
        policy.inactivity_due_at = None
        self._cancel_pending_jobs(ctx, public_only=False)
        return _Outcome(
            TransitionName.CLOSE_INACTIVE,
            IssueState.CLOSED_INACTIVE,
            "closed after reminders; reporter may reopen",
        )

    def _on_duplicate(self, ctx: _Context) -> _Outcome:
        original = self._payload_int(ctx, "duplicate_of")
        self._cancel_pending_jobs(ctx, public_only=False)
        return _Outcome(
            TransitionName.MARK_DUPLICATE, IssueState.DUPLICATE, f"duplicate of #{original}"
        )

    def _on_automation_failure(self, ctx: _Context) -> _Outcome:
        reason = self._payload_str(ctx, "reason", "automation defect")
        for session in ctx.uow.list_sessions(issue_id=ctx.issue.id):
            if session.state not in SESSION_TERMINAL:
                self._finish_session(
                    ctx, session, SessionState.NEEDS_ATTENTION, "Automation failure", reason
                )
        self._enqueue(ctx, JobKind.RECOVERY, suffix=str(ctx.issue.version))
        return _Outcome(TransitionName.AUTOMATION_FAILED, IssueState.AUTOMATION_ERROR, reason)

    def _on_retry(self, ctx: _Context) -> _Outcome:
        if ctx.event.actor.role != ActorRole.OPERATOR:
            raise DomainError(ErrorCode.HUMAN_GATE_REQUIRED, "retry requires an operator")
        if ctx.issue.state == IssueState.BLOCKED_ENVIRONMENT:
            return self._start_reproduction(ctx, "operator retried after environment block")
        target = ctx.issue.previous_state
        if target is None:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION, "no recoverable state recorded before the failure"
            )
        for session in ctx.uow.list_sessions(issue_id=ctx.issue.id):
            if session.state not in SESSION_TERMINAL:
                self._finish_session(
                    ctx, session, SessionState.FAILED, "Superseded", "operator retry"
                )
        if target == IssueState.REPRODUCING:
            return self._start_reproduction(ctx, "operator retried after automation failure")
        if target in (IssueState.FIXING, IssueState.FIX_AUTHORIZED):
            try:
                authorization = self._active_fix_authorization(ctx)
            except DomainError:
                return _Outcome(
                    TransitionName.RETRY,
                    IssueState.NEEDS_OWNER_DECISION,
                    "fix authorization expired; owner must confirm again",
                )
            self._queue_fix_session(
                ctx,
                authorization,
                f"operator retry under {authorization.actor.login}'s authorization",
                job_suffix=f"retry:{ctx.issue.version}",
            )
            return _Outcome(
                TransitionName.RETRY,
                IssueState.FIX_AUTHORIZED,
                "bounded fix session re-queued under the existing authorization",
            )
        if target not in TRANSITIONS[TransitionName.RETRY].destinations:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION,
                "previous state is not a safe gate to restore",
                {"previous": target.value},
            )
        if target == IssueState.TRIAGE:
            self._enqueue(ctx, JobKind.CLASSIFY, suffix=f"retry:{ctx.issue.version}")
        return _Outcome(TransitionName.RETRY, target, f"restored safe gate {target.value}")

    def _on_session_cancel(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        if ctx.event.actor.role != ActorRole.OPERATOR:
            raise DomainError(ErrorCode.UNAUTHORIZED_ACTOR, "only operators may cancel sessions")
        if session.state in SESSION_TERMINAL:
            return _Outcome(None, None, "session already finished")
        session.cancel_requested = True
        session.updated_at = ctx.now
        if session.state == SessionState.QUEUED:
            self._finish_session(
                ctx, session, SessionState.CANCELLED, "Cancelled", "cancelled before start"
            )
            return _Outcome(None, None, "queued session cancelled")
        self._save_session(ctx, session)
        self._enqueue(
            ctx,
            JobKind.CANCEL_SESSION,
            suffix=str(session.id),
            payload={"session_id": str(session.id)},
        )
        return _Outcome(None, None, "bounded cancellation requested")

    def _on_session_message(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
        if ctx.event.actor.role != ActorRole.OPERATOR:
            raise DomainError(
                ErrorCode.UNAUTHORIZED_ACTOR, "only operators may message agent sessions"
            )
        body = self._payload_str(ctx, "body")
        if not body.strip():
            raise DomainError(ErrorCode.INVALID_INPUT, "message body is empty")
        if session.state in SESSION_TERMINAL:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION, "session is no longer accepting messages"
            )
        message = ConversationMessage(
            session_id=session.id,
            author=ConversationAuthor.OPERATOR,
            author_login=ctx.event.actor.login,
            body=body,
            created_at=ctx.now,
        )
        ctx.uow.add_message(message)
        session.updated_at = ctx.now
        self._save_session(ctx, session)
        self._enqueue(
            ctx,
            JobKind.DELIVER_SESSION_MESSAGE,
            suffix=str(message.id),
            payload={"session_id": str(session.id), "message_id": str(message.id)},
        )
        return _Outcome(None, None, "message queued for delivery")
