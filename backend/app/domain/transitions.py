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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.errors import DomainError, ErrorCode
from app.domain.models import (
    PUBLIC_JOB_KINDS,
    Actor,
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
    Repository,
    RepositoryScope,
    ReviewVerdict,
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
            EventType.PR_MERGED: self._on_pr_merged,
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
        self, uow: UnitOfWork, event: Event, *, expected_version: int | None = None
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
            if session.state == SessionState.QUEUED and ctx.issue.state not in (
                IssueState.FIX_AUTHORIZED,
            ):
                session.state = SessionState.CANCELLED
                session.finished_at = ctx.now
            session.workspace_released = True
            session.workspace_name = None
            session.updated_at = ctx.now
            ctx.uow.save_session(session)

    # --------------------------------------------------------------- helpers

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
            created_at=ctx.now,
        )
        ctx.uow.add_job(job)
        return job

    def _cancel_pending_jobs(self, ctx: _Context, public_only: bool) -> None:
        for job in ctx.uow.list_jobs(ctx.issue.id):
            if job.status != JobStatus.PENDING:
                continue
            if public_only and job.kind not in PUBLIC_JOB_KINDS:
                continue
            job.status = JobStatus.CANCELLED
            ctx.uow.save_job(job)

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
        session.workspace_released = True
        session.workspace_name = None
        session.finished_at = ctx.now
        session.updated_at = ctx.now
        session.progress_percent = (
            100 if state == SessionState.COMPLETED else session.progress_percent
        )
        session.current_action = detail
        ctx.uow.save_session(session)
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
            self._enqueue(ctx, JobKind.REMINDER, run_after=ctx.now + self.reminder_after)
            self._enqueue(ctx, JobKind.INACTIVITY, run_after=ctx.now + self.inactivity_after)
            return _Outcome(
                TransitionName.REQUEST_INFORMATION,
                IssueState.AWAITING_REPORTER,
                f"{created} focused question(s) requested",
            )
        return self._start_reproduction(ctx, "classification found reproduction context complete")

    def _on_reporter_response(self, ctx: _Context) -> _Outcome:
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
        session.current_action = "Building fixture without reporter data"
        session.next_checkpoint = "Attach minimal fixture and result matrix"
        session.updated_at = ctx.now
        ctx.uow.save_session(session)
        self._enqueue(ctx, JobKind.START_REPRODUCTION, payload={"session_id": str(session.id)})
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
        progress = self._payload_int(ctx, "progress_percent", session.progress_percent)
        session.progress_percent = max(0, min(100, progress))
        session.current_action = self._payload_str(ctx, "current_action", session.current_action)
        session.next_checkpoint = self._payload_str(ctx, "next_checkpoint", session.next_checkpoint)
        session.last_heartbeat_at = ctx.now
        session.updated_at = ctx.now
        ctx.uow.save_session(session)
        label = ctx.event.payload.get("label")
        if isinstance(label, str):
            ctx.uow.add_session_event(
                SessionEvent(
                    session_id=session.id,
                    label=label,
                    detail=session.current_action,
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
            revision = self._latest_revision(ctx)
            self._create_session(
                ctx,
                SessionKind.FIX,
                f"Fix {ctx.issue.key}",
                f"{ctx.event.actor.login} confirmed expected behavior",
                SessionBudget(
                    wall_clock_seconds=5400,
                    allowed_capabilities=["read_repo", "run_isolated_tests", "open_pull_request"],
                ),
                target_commit=revision.target_commit if revision else None,
            )
            self._enqueue(ctx, JobKind.START_FIX)
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
        if kind == DecisionKind.APPROVE_PR:
            ctx.uow.add_decision(decision)
            self._mark_pr_approval(ctx, True, ctx.event.actor.login)
            return _Outcome(
                TransitionName.OWNER_APPROVED,
                IssueState.AWAITING_OWNER,
                "human approval recorded; merge remains a human GitHub action",
            )
        ctx.uow.add_decision(decision)
        self._mark_pr_approval(ctx, False, None)
        return _Outcome(
            TransitionName.OWNER_REQUESTED_CHANGES, IssueState.CHANGES_REQUESTED, rationale
        )

    def _mark_pr_approval(self, ctx: _Context, approved: bool, approver: str | None) -> None:
        prs = ctx.uow.list_pull_requests(ctx.issue.id)
        if not prs:
            raise DomainError(ErrorCode.ILLEGAL_TRANSITION, "no pull request exists for this issue")
        pr = prs[-1]
        pr.human_approved = approved
        pr.human_approver = approver
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)

    def _on_human_review(self, ctx: _Context) -> _Outcome:
        state = self._payload_str(ctx, "state")
        reviewer = self._payload_str(ctx, "reviewer", ctx.event.actor.login)
        if ctx.event.actor.role != ActorRole.OWNER:
            raise DomainError(ErrorCode.HUMAN_GATE_REQUIRED, "reviews must come from a human owner")
        if state == "approved":
            ctx.uow.add_decision(
                HumanDecision(
                    issue_id=ctx.issue.id,
                    issue_revision=ctx.issue.revision,
                    kind=DecisionKind.APPROVE_PR,
                    actor=Actor(role=ActorRole.OWNER, login=reviewer),
                    created_at=ctx.now,
                )
            )
            self._mark_pr_approval(ctx, True, reviewer)
            return _Outcome(TransitionName.OWNER_APPROVED, IssueState.AWAITING_OWNER, reviewer)
        if state == "changes_requested":
            self._mark_pr_approval(ctx, False, None)
            return _Outcome(
                TransitionName.OWNER_REQUESTED_CHANGES, IssueState.CHANGES_REQUESTED, reviewer
            )
        raise DomainError(
            ErrorCode.INVALID_INPUT, "review state must be approved or changes_requested"
        )

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

    def _on_fix_session_started(self, ctx: _Context) -> _Outcome:
        authorization = self._active_fix_authorization(ctx)
        revision = self._latest_revision(ctx)
        session = self._create_session(
            ctx,
            SessionKind.FIX,
            f"Fix {ctx.issue.key}",
            f"{authorization.actor.login} confirmed expected behavior",
            SessionBudget(
                wall_clock_seconds=5400,
                allowed_capabilities=["read_repo", "run_isolated_tests", "open_pull_request"],
            ),
            target_commit=revision.target_commit if revision else None,
        )
        session.state = SessionState.RUNNING
        session.workspace_released = False
        session.workspace_name = f"superset-fix-{ctx.issue.external_number}"
        session.branch = f"devin/{ctx.issue.external_number}-fix"
        session.started_at = ctx.now
        session.last_heartbeat_at = ctx.now
        session.current_action = "Writing a failing regression test"
        session.next_checkpoint = "Open a draft pull request against exloong/superset"
        session.updated_at = ctx.now
        ctx.uow.save_session(session)
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
        candidates_raw = raw_pr.get("reviewer_candidates", [])
        candidates = (
            [c for c in candidates_raw if isinstance(c, str)]
            if isinstance(candidates_raw, list)
            else []
        )
        pr = PullRequestState(
            issue_id=ctx.issue.id,
            session_id=session.id,
            repository=repository,
            number=number,
            head_branch=head_branch,
            head_sha=head_sha,
            url=url,
            reviewer_candidates=candidates,
            reviewer_routing_rule=".github/CODEOWNERS + owner_team" if candidates else None,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        ctx.uow.add_pull_request(pr)
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
        self._enqueue(
            ctx, JobKind.TRIGGER_DEVIN_REVIEW, suffix=head_sha, payload={"pr_number": number}
        )
        self._enqueue(
            ctx,
            JobKind.REQUEST_REVIEWERS,
            suffix=str(number),
            payload={"pr_number": number, "candidates": candidates},
        )
        return _Outcome(TransitionName.OPEN_PR, IssueState.PR_OPEN, f"PR #{number} opened")

    def _on_pr_opened_webhook(self, ctx: _Context) -> _Outcome:
        prs = ctx.uow.list_pull_requests(ctx.issue.id)
        number = self._payload_int(ctx, "number")
        if not any(pr.number == number for pr in prs):
            raise DomainError(
                ErrorCode.INVALID_INPUT, "pull request was not created by a Relay fix session"
            )
        return _Outcome(None, None, f"PR #{number} webhook acknowledged")

    def _on_devin_review(self, ctx: _Context) -> _Outcome:
        prs = ctx.uow.list_pull_requests(ctx.issue.id)
        if not prs:
            raise DomainError(ErrorCode.ILLEGAL_TRANSITION, "no pull request to review")
        pr = prs[-1]
        verdict_raw = self._payload_str(ctx, "verdict")
        try:
            pr.devin_review = ReviewVerdict(verdict_raw)
        except ValueError as exc:
            raise DomainError(ErrorCode.INVALID_INPUT, "invalid review verdict") from exc
        pr.devin_review_findings = self._payload_int(ctx, "findings", 0)
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
        prs = ctx.uow.list_pull_requests(ctx.issue.id)
        if not prs:
            raise DomainError(ErrorCode.ILLEGAL_TRANSITION, "no pull request to merge")
        pr = prs[-1]
        if not pr.human_approved:
            raise DomainError(
                ErrorCode.HUMAN_GATE_REQUIRED,
                "merge observed without a recorded human approval; Devin Review is not approval",
            )
        pr.merged = True
        pr.updated_at = ctx.now
        ctx.uow.save_pull_request(pr)
        self._cancel_pending_jobs(ctx, public_only=False)
        return _Outcome(TransitionName.COMPLETE, IssueState.COMPLETED, f"PR #{pr.number} merged")

    def _on_reminder(self, ctx: _Context) -> _Outcome:
        if not self._open_required_questions(ctx):
            return _Outcome(None, None, "no open questions; reminder skipped")
        self._enqueue(ctx, JobKind.REMINDER, suffix=f"sent:{ctx.issue.version}")
        return _Outcome(
            TransitionName.REMIND_REPORTER, IssueState.AWAITING_REPORTER, "reminder published"
        )

    def _on_inactivity(self, ctx: _Context) -> _Outcome:
        if not self._open_required_questions(ctx):
            return _Outcome(None, None, "no open questions; inactivity skipped")
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
        if target is None or target not in TRANSITIONS[TransitionName.RETRY].destinations:
            raise DomainError(
                ErrorCode.ILLEGAL_TRANSITION, "no recoverable state recorded before the failure"
            )
        return _Outcome(TransitionName.RETRY, target, f"restored {target.value}")

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
        ctx.uow.save_session(session)
        self._enqueue(
            ctx,
            JobKind.CANCEL_SESSION,
            suffix=str(session.id),
            payload={"session_id": str(session.id)},
        )
        return _Outcome(None, None, "bounded cancellation requested")

    def _on_session_message(self, ctx: _Context) -> _Outcome:
        session = self._payload_session(ctx)
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
        self._enqueue(
            ctx,
            JobKind.DELIVER_SESSION_MESSAGE,
            suffix=str(message.id),
            payload={"session_id": str(session.id), "message_id": str(message.id)},
        )
        return _Outcome(None, None, "message queued for delivery")
