from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Table, and_, func, select, text
from sqlalchemy.engine import Connection, Engine

from app.domain.models import (
    AgentSession,
    AuditEntry,
    ConversationMessage,
    Event,
    Evidence,
    HumanDecision,
    InformationRequest,
    Issue,
    IssueRevision,
    Job,
    PullRequestState,
    Repository,
    ReviewerRouting,
    SessionEvent,
    SessionOutput,
    TransitionAttempt,
)
from app.domain.states import IssueState, SessionState
from app.persistence import tables as t

M = TypeVar("M", bound=BaseModel)


def _dump(record: BaseModel) -> dict[str, object]:
    return record.model_dump(mode="json")


class SqlAlchemyUnitOfWork:
    """One connection + one transaction. Satisfies ``app.domain.ports.UnitOfWork``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._conn: Connection | None = None

    # ------------------------------------------------------------ lifecycle

    @property
    def conn(self) -> Connection:
        if self._conn is None:
            self._conn = self._engine.connect()
            self._conn.begin()
        return self._conn

    def __enter__(self) -> SqlAlchemyUnitOfWork:
        _ = self.conn
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._conn is None:
            return
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self._conn.close()
            self._conn = None

    def commit(self) -> None:
        if self._conn is not None and self._conn.in_transaction():
            self._conn.commit()
        if self._conn is not None:
            self._conn.begin()

    def rollback(self) -> None:
        if self._conn is not None and self._conn.in_transaction():
            self._conn.rollback()
        if self._conn is not None:
            self._conn.begin()

    def lock_issue(self, issue_id: uuid.UUID) -> None:
        dialect = self.conn.dialect.name
        if dialect == "postgresql":
            self.conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": str(issue_id)}
            )
            self.conn.execute(
                select(t.issues.c.id).where(t.issues.c.id == str(issue_id)).with_for_update()
            )
        else:
            # SQLite serializes writers at the database level; touching the row
            # inside the open transaction is sufficient for per-issue ordering.
            self.conn.execute(select(t.issues.c.id).where(t.issues.c.id == str(issue_id)))

    # -------------------------------------------------------------- helpers

    def _insert(self, table: Table, record: BaseModel, **columns: object) -> None:
        self.conn.execute(table.insert().values(data=_dump(record), **columns))

    def _update(
        self, table: Table, record_id: uuid.UUID, record: BaseModel, **columns: object
    ) -> None:
        self.conn.execute(
            table.update().where(table.c.id == str(record_id)).values(data=_dump(record), **columns)
        )

    def _one(self, table: Table, model: type[M], *where: ColumnElement[bool]) -> M | None:
        row = self.conn.execute(select(table.c.data).where(and_(*where))).first()
        return model.model_validate(row[0]) if row else None

    def _many(
        self,
        table: Table,
        model: type[M],
        *where: ColumnElement[bool],
        order_by: ColumnElement[object] | None = None,
    ) -> list[M]:
        stmt = select(table.c.data)
        if where:
            stmt = stmt.where(and_(*where))
        if order_by is not None:
            stmt = stmt.order_by(order_by, table.c.id)
        else:
            stmt = stmt.order_by(table.c.id)
        return [model.model_validate(row[0]) for row in self.conn.execute(stmt)]

    # ----------------------------------------------------------- repository

    def get_repository(self) -> Repository | None:
        rows = self._many(t.repositories, Repository)
        return rows[0] if rows else None

    def add_repository(self, repository: Repository) -> None:
        self._insert(
            t.repositories, repository, id=str(repository.id), full_name=repository.full_name
        )

    # ---------------------------------------------------------------- issues

    def _issue_columns(self, issue: Issue) -> dict[str, object]:
        return {
            "repository_id": str(issue.repository_id),
            "external_number": issue.external_number,
            "state": issue.state.value,
            "owner_team": issue.owner_team,
            "version": issue.version,
            "revision": issue.revision,
            "updated_at": issue.updated_at,
        }

    def add_issue(self, issue: Issue) -> None:
        self._insert(t.issues, issue, id=str(issue.id), **self._issue_columns(issue))

    def save_issue(self, issue: Issue) -> None:
        self._update(t.issues, issue.id, issue, **self._issue_columns(issue))

    def get_issue(self, issue_id: uuid.UUID) -> Issue | None:
        return self._one(t.issues, Issue, t.issues.c.id == str(issue_id))

    def get_issue_by_number(self, repository_id: uuid.UUID, number: int) -> Issue | None:
        return self._one(
            t.issues,
            Issue,
            t.issues.c.repository_id == str(repository_id),
            t.issues.c.external_number == number,
        )

    def list_issues(
        self, *, state: IssueState | None = None, owner_team: str | None = None
    ) -> Sequence[Issue]:
        where: list[ColumnElement[bool]] = []
        if state is not None:
            where.append(t.issues.c.state == state.value)
        if owner_team is not None:
            where.append(t.issues.c.owner_team == owner_team)
        return self._many(t.issues, Issue, *where, order_by=t.issues.c.updated_at.desc())

    def add_revision(self, revision: IssueRevision) -> None:
        self._insert(
            t.issue_revisions,
            revision,
            id=str(revision.id),
            issue_id=str(revision.issue_id),
            revision=revision.revision,
        )

    def list_revisions(self, issue_id: uuid.UUID) -> Sequence[IssueRevision]:
        return self._many(
            t.issue_revisions,
            IssueRevision,
            t.issue_revisions.c.issue_id == str(issue_id),
            order_by=t.issue_revisions.c.revision,
        )

    # ---------------------------------------------------------------- events

    def add_event(self, event: Event) -> None:
        self._insert(
            t.events,
            event,
            id=str(event.id),
            issue_id=str(event.issue_id) if event.issue_id else None,
            source=event.source,
            delivery_id=event.delivery_id,
            type=event.type.value,
            received_at=event.received_at,
        )

    def get_event_by_delivery(self, source: str, delivery_id: str) -> Event | None:
        return self._one(
            t.events, Event, t.events.c.source == source, t.events.c.delivery_id == delivery_id
        )

    def list_events(self, issue_id: uuid.UUID) -> Sequence[Event]:
        return self._many(
            t.events, Event, t.events.c.issue_id == str(issue_id), order_by=t.events.c.received_at
        )

    def count_events(self) -> int:
        value = self.conn.execute(select(func.count()).select_from(t.events)).scalar()
        return int(value or 0)

    # -------------------------------------------------------------- attempts

    def add_attempt(self, attempt: TransitionAttempt) -> None:
        self._insert(
            t.transition_attempts,
            attempt,
            id=str(attempt.id),
            issue_id=str(attempt.issue_id),
            event_id=str(attempt.event_id),
            status=attempt.status.value,
            created_at=attempt.created_at,
        )

    def list_attempts(self, issue_id: uuid.UUID) -> Sequence[TransitionAttempt]:
        return self._many(
            t.transition_attempts,
            TransitionAttempt,
            t.transition_attempts.c.issue_id == str(issue_id),
            order_by=t.transition_attempts.c.created_at,
        )

    def get_attempt_for_event(self, event_id: uuid.UUID) -> TransitionAttempt | None:
        return self._one(
            t.transition_attempts,
            TransitionAttempt,
            t.transition_attempts.c.event_id == str(event_id),
        )

    def list_all_attempts(self) -> Sequence[TransitionAttempt]:
        return self._many(
            t.transition_attempts, TransitionAttempt, order_by=t.transition_attempts.c.created_at
        )

    # ------------------------------------------------------------- questions

    def add_question(self, question: InformationRequest) -> None:
        self._insert(
            t.information_requests,
            question,
            id=str(question.id),
            issue_id=str(question.issue_id),
            issue_revision=question.issue_revision,
            status=question.status.value,
            created_at=question.created_at,
        )

    def save_question(self, question: InformationRequest) -> None:
        self._update(t.information_requests, question.id, question, status=question.status.value)

    def get_question(self, question_id: uuid.UUID) -> InformationRequest | None:
        return self._one(
            t.information_requests,
            InformationRequest,
            t.information_requests.c.id == str(question_id),
        )

    def list_questions(
        self, issue_id: uuid.UUID, revision: int | None = None
    ) -> Sequence[InformationRequest]:
        where: list[ColumnElement[bool]] = [t.information_requests.c.issue_id == str(issue_id)]
        if revision is not None:
            where.append(t.information_requests.c.issue_revision == revision)
        return self._many(
            t.information_requests,
            InformationRequest,
            *where,
            order_by=t.information_requests.c.created_at,
        )

    # -------------------------------------------------------------- evidence

    def add_evidence(self, evidence: Evidence) -> None:
        self._insert(
            t.artifacts,
            evidence,
            id=str(evidence.id),
            issue_id=str(evidence.issue_id),
            session_id=str(evidence.session_id) if evidence.session_id else None,
            created_at=evidence.created_at,
        )

    def list_evidence(self, issue_id: uuid.UUID) -> Sequence[Evidence]:
        return self._many(
            t.artifacts,
            Evidence,
            t.artifacts.c.issue_id == str(issue_id),
            order_by=t.artifacts.c.created_at,
        )

    # ------------------------------------------------------------- decisions

    def add_decision(self, decision: HumanDecision) -> None:
        self._insert(
            t.human_decisions,
            decision,
            id=str(decision.id),
            issue_id=str(decision.issue_id),
            created_at=decision.created_at,
        )

    def list_decisions(self, issue_id: uuid.UUID) -> Sequence[HumanDecision]:
        return self._many(
            t.human_decisions,
            HumanDecision,
            t.human_decisions.c.issue_id == str(issue_id),
            order_by=t.human_decisions.c.created_at,
        )

    # ------------------------------------------------------------------ jobs

    def add_job(self, job: Job) -> None:
        self._insert(
            t.jobs,
            job,
            id=str(job.id),
            issue_id=str(job.issue_id) if job.issue_id else None,
            kind=job.kind.value,
            idempotency_key=job.idempotency_key,
            status=job.status.value,
            run_after=job.run_after,
            created_at=job.created_at,
        )

    def save_job(self, job: Job) -> None:
        self._update(t.jobs, job.id, job, status=job.status.value, run_after=job.run_after)

    def get_job(self, job_id: uuid.UUID) -> Job | None:
        return self._one(t.jobs, Job, t.jobs.c.id == str(job_id))

    def get_job_by_key(self, idempotency_key: str) -> Job | None:
        return self._one(t.jobs, Job, t.jobs.c.idempotency_key == idempotency_key)

    def list_jobs(self, issue_id: uuid.UUID | None = None) -> Sequence[Job]:
        where: list[ColumnElement[bool]] = []
        if issue_id is not None:
            where.append(t.jobs.c.issue_id == str(issue_id))
        return self._many(t.jobs, Job, *where, order_by=t.jobs.c.created_at)

    # -------------------------------------------------------------- sessions

    def add_session(self, session: AgentSession) -> None:
        self._insert(
            t.agent_sessions,
            session,
            id=str(session.id),
            issue_id=str(session.issue_id),
            state=session.state.value,
            workspace_released=session.workspace_released,
            created_at=session.created_at,
        )

    def save_session(self, session: AgentSession) -> None:
        self._update(
            t.agent_sessions,
            session.id,
            session,
            state=session.state.value,
            workspace_released=session.workspace_released,
        )

    def get_session(self, session_id: uuid.UUID) -> AgentSession | None:
        return self._one(t.agent_sessions, AgentSession, t.agent_sessions.c.id == str(session_id))

    def list_sessions(
        self, *, issue_id: uuid.UUID | None = None, state: SessionState | None = None
    ) -> Sequence[AgentSession]:
        where: list[ColumnElement[bool]] = []
        if issue_id is not None:
            where.append(t.agent_sessions.c.issue_id == str(issue_id))
        if state is not None:
            where.append(t.agent_sessions.c.state == state.value)
        return self._many(
            t.agent_sessions, AgentSession, *where, order_by=t.agent_sessions.c.created_at
        )

    def add_session_event(self, event: SessionEvent) -> None:
        self._insert(
            t.session_events,
            event,
            id=str(event.id),
            session_id=str(event.session_id),
            created_at=event.created_at,
        )

    def list_session_events(self, session_id: uuid.UUID) -> Sequence[SessionEvent]:
        return self._many(
            t.session_events,
            SessionEvent,
            t.session_events.c.session_id == str(session_id),
            order_by=t.session_events.c.created_at,
        )

    def add_message(self, message: ConversationMessage) -> None:
        self._insert(
            t.conversation_messages,
            message,
            id=str(message.id),
            session_id=str(message.session_id),
            created_at=message.created_at,
        )

    def list_messages(self, session_id: uuid.UUID) -> Sequence[ConversationMessage]:
        return self._many(
            t.conversation_messages,
            ConversationMessage,
            t.conversation_messages.c.session_id == str(session_id),
            order_by=t.conversation_messages.c.created_at,
        )

    def add_output(self, output: SessionOutput) -> None:
        self._insert(
            t.session_outputs,
            output,
            id=str(output.id),
            session_id=str(output.session_id),
            created_at=output.created_at,
        )

    def list_outputs(self, session_id: uuid.UUID) -> Sequence[SessionOutput]:
        return self._many(
            t.session_outputs,
            SessionOutput,
            t.session_outputs.c.session_id == str(session_id),
            order_by=t.session_outputs.c.created_at,
        )

    # --------------------------------------------------------- pull requests

    def add_pull_request(self, pr: PullRequestState) -> None:
        self._insert(
            t.pull_requests,
            pr,
            id=str(pr.id),
            issue_id=str(pr.issue_id),
            number=pr.number,
            created_at=pr.created_at,
        )

    def save_pull_request(self, pr: PullRequestState) -> None:
        self._update(t.pull_requests, pr.id, pr)

    def list_pull_requests(self, issue_id: uuid.UUID) -> Sequence[PullRequestState]:
        return self._many(
            t.pull_requests,
            PullRequestState,
            t.pull_requests.c.issue_id == str(issue_id),
            order_by=t.pull_requests.c.created_at,
        )

    # ------------------------------------------------------ reviewer routing

    def add_reviewer_routing(self, routing: ReviewerRouting) -> None:
        self._insert(
            t.reviewer_routings,
            routing,
            id=str(routing.id),
            issue_id=str(routing.issue_id),
            pull_request_number=routing.pull_request_number,
            head_sha=routing.head_sha,
            created_at=routing.created_at,
        )

    def list_reviewer_routings(self, issue_id: uuid.UUID) -> Sequence[ReviewerRouting]:
        return self._many(
            t.reviewer_routings,
            ReviewerRouting,
            t.reviewer_routings.c.issue_id == str(issue_id),
            order_by=t.reviewer_routings.c.created_at,
        )

    # ----------------------------------------------------------------- audit

    def add_audit(self, entry: AuditEntry) -> None:
        self._insert(
            t.audit_entries,
            entry,
            id=str(entry.id),
            correlation_id=entry.correlation_id,
            created_at=entry.created_at,
        )

    def list_audit(self, correlation_id: str) -> Sequence[AuditEntry]:
        return self._many(
            t.audit_entries,
            AuditEntry,
            t.audit_entries.c.correlation_id == correlation_id,
            order_by=t.audit_entries.c.created_at,
        )


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.engine)
