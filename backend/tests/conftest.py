from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from app.api import create_app
from app.domain.models import Actor, Event, Issue
from app.domain.states import TARGET_REPOSITORY, ActorRole, EventType
from app.domain.transitions import TransitionResult, TransitionService
from app.persistence.database import make_engine, upgrade
from app.persistence.sqlalchemy_uow import SqlAlchemyUnitOfWorkFactory
from fastapi import FastAPI
from fastapi.testclient import TestClient


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


class Harness:
    """Drives the transition service directly against an in-memory database."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.engine = make_engine("sqlite:///:memory:")
        upgrade(self.engine)
        self.uow = SqlAlchemyUnitOfWorkFactory(self.engine)
        self.service = TransitionService(clock=self.clock)
        self._seq = 0

    def event(
        self,
        type: EventType,
        *,
        issue_id: uuid.UUID | None = None,
        role: ActorRole = ActorRole.SYSTEM,
        login: str = "tester",
        delivery_id: str | None = None,
        revision: int | None = None,
        **payload: object,
    ) -> Event:
        self._seq += 1
        return Event(
            issue_id=issue_id,
            source="test",
            delivery_id=delivery_id or f"d{self._seq}",
            type=type,
            actor=Actor(role=role, login=login),
            issue_revision=revision,
            payload=payload,
        )

    def apply(self, event: Event, *, expected_version: int | None = None) -> TransitionResult:
        with self.uow() as uow:
            return self.service.apply(uow, event, expected_version=expected_version)

    def issue(self, issue_id: uuid.UUID) -> Issue:
        with self.uow() as uow:
            found = uow.get_issue(issue_id)
        assert found is not None
        return found

    def open_issue(
        self,
        number: int = 100,
        *,
        title: str = "Chart export fails",
        body: str = "Steps: open a chart, export CSV.",
        labels: list[str] | None = None,
        delivery_id: str | None = None,
    ) -> Issue:
        result = self.apply(
            self.event(
                EventType.ISSUE_OPENED,
                delivery_id=delivery_id,
                repository=TARGET_REPOSITORY,
                number=number,
                title=title,
                body=body,
                reporter="reporter-1",
                labels=labels or [],
                target_commit="abc123",
            )
        )
        assert result.issue is not None
        return result.issue

    def classify(self, issue: Issue, missing: list[dict[str, str]] | None = None) -> Issue:
        result = self.apply(
            self.event(
                EventType.CLASSIFICATION_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                missing_fields=missing or [],
            )
        )
        assert result.issue is not None
        return result.issue

    def latest_session_id(self, issue_id: uuid.UUID) -> uuid.UUID:
        with self.uow() as uow:
            sessions = uow.list_sessions(issue_id=issue_id)
        assert sessions
        return sessions[-1].id

    def reproduce(self, issue: Issue) -> Issue:
        result = self.apply(
            self.event(
                EventType.REPRODUCTION_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                session_id=str(self.latest_session_id(issue.id)),
                reproduced=True,
                evidence=[{"kind": "result_matrix", "title": "Matrix"}],
            )
        )
        assert result.issue is not None
        return result.issue

    def confirm(self, issue: Issue) -> Issue:
        result = self.apply(
            self.event(
                EventType.OWNER_DECISION,
                issue_id=issue.id,
                role=ActorRole.OWNER,
                login="owner-1",
                decision="confirm_bug",
                rationale="Expected behavior confirmed.",
            )
        )
        assert result.issue is not None
        return result.issue

    def start_fix(self, issue: Issue) -> Issue:
        result = self.apply(self.event(EventType.FIX_SESSION_STARTED, issue_id=issue.id))
        assert result.issue is not None
        return result.issue

    def open_pr(self, issue: Issue, repository: str = TARGET_REPOSITORY) -> Issue:
        result = self.apply(
            self.event(
                EventType.FIX_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                session_id=str(self.latest_session_id(issue.id)),
                pull_request={
                    "repository": repository,
                    "number": 555,
                    "head_branch": "devin/100-fix",
                    "head_sha": "deadbeef",
                    "url": f"https://github.com/{repository}/pull/555",
                },
            )
        )
        assert result.issue is not None
        return result.issue

    def to_pr_open(self) -> Issue:
        issue = self.open_issue()
        issue = self.classify(issue)
        issue = self.reproduce(issue)
        issue = self.confirm(issue)
        issue = self.start_fix(issue)
        return self.open_pr(issue)


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(clock: FakeClock) -> FastAPI:
    return create_app(database_url="sqlite:///:memory:", seed_scenarios=True, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
