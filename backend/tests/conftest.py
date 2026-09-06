from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from app.api import create_app
from app.api.deps import AuthConfig, Principal, StaticTokenAuthenticator
from app.domain.models import Actor, Event, Issue, JobKind, PullRequestState
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
        extra: dict[str, object] | None = None,
        **payload: object,
    ) -> Event:
        self._seq += 1
        if extra:
            payload = {**payload, **extra}
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

    def open_pr(
        self,
        issue: Issue,
        repository: str = TARGET_REPOSITORY,
        head_sha: str = "deadbeef",
        **extra_pr_fields: object,
    ) -> Issue:
        pull_request: dict[str, object] = {
            "repository": repository,
            "number": 555,
            "head_branch": "devin/100-fix",
            "head_sha": head_sha,
            "url": f"https://github.com/{repository}/pull/555",
        }
        pull_request.update(extra_pr_fields)
        result = self.apply(
            self.event(
                EventType.FIX_RESULT,
                issue_id=issue.id,
                role=ActorRole.AGENT,
                session_id=str(self.latest_session_id(issue.id)),
                pull_request=pull_request,
            )
        )
        assert result.issue is not None
        return result.issue

    def pr(self, issue_id: uuid.UUID) -> PullRequestState:
        with self.uow() as uow:
            prs = uow.list_pull_requests(issue_id)
        assert prs
        return prs[-1]

    def route_reviewers(
        self,
        issue: Issue,
        *,
        members: list[str] | None = None,
        state: str = "resolved",
        head_sha: str | None = None,
        role: ActorRole = ActorRole.SYSTEM,
    ) -> TransitionResult:
        pr = self.pr(issue.id)
        return self.apply(
            self.event(
                EventType.REVIEWER_ROUTING_RESOLVED,
                issue_id=issue.id,
                role=role,
                login="codeowners-adapter",
                pr_number=pr.number,
                head_sha=head_sha or pr.head_sha,
                state=state,
                candidates=[
                    {
                        "team": "core",
                        "members": members if members is not None else ["owner-1"],
                        "rule": "superset/** @core",
                        "paths": ["superset/x.py"],
                    }
                ],
                unowned_paths=[] if state == "resolved" else ["docs/new.md"],
                ambiguous_paths=[],
                rationale="deterministic CODEOWNERS match",
            )
        )

    def review(
        self,
        issue: Issue,
        *,
        state: str = "approved",
        login: str = "owner-1",
        role: ActorRole = ActorRole.OWNER,
        head_sha: str | None = None,
        **payload: object,
    ) -> TransitionResult:
        pr = self.pr(issue.id)
        body: dict[str, object] = {
            "pr_number": pr.number,
            "head_sha": head_sha or pr.head_sha,
            "state": state,
            **payload,
        }
        return self.apply(
            self.event(
                EventType.HUMAN_REVIEW_SUBMITTED,
                issue_id=issue.id,
                role=role,
                login=login,
                extra=body,
            )
        )

    def merge(self, issue: Issue, head_sha: str | None = None) -> TransitionResult:
        pr = self.pr(issue.id)
        return self.apply(
            self.event(
                EventType.PR_MERGED,
                issue_id=issue.id,
                pr_number=pr.number,
                head_sha=head_sha or pr.head_sha,
            )
        )

    def timer(
        self,
        issue: Issue,
        kind: EventType,
        *,
        advance: bool = True,
        policy_revision: int | None = None,
        due_at: str | None = None,
    ) -> TransitionResult:
        policy = self.issue(issue.id).reporter_wait
        assert policy is not None
        scheduled = (
            policy.reminder_due_at
            if kind == EventType.REMINDER_ELAPSED
            else policy.inactivity_due_at
        )
        if advance and scheduled is not None and self.clock.current < scheduled:
            self.clock.current = scheduled
        return self.apply(
            self.event(
                kind,
                issue_id=issue.id,
                policy_revision=(
                    policy_revision if policy_revision is not None else policy.policy_revision
                ),
                due_at=due_at or (scheduled.isoformat() if scheduled else ""),
            )
        )

    def jobs(self, issue_id: uuid.UUID, kind: JobKind | None = None) -> list[str]:
        with self.uow() as uow:
            jobs = uow.list_jobs(issue_id)
        return [j.idempotency_key for j in jobs if kind is None or j.kind == kind]

    def to_pr_open(self) -> Issue:
        issue = self.open_issue()
        issue = self.classify(issue)
        issue = self.reproduce(issue)
        issue = self.confirm(issue)
        issue = self.start_fix(issue)
        return self.open_pr(issue)

    def to_routed_pr(self) -> Issue:
        issue = self.to_pr_open()
        assert self.route_reviewers(issue).attempt.status.value == "noop"
        return self.issue(issue.id)


@pytest.fixture
def h() -> Harness:
    return Harness()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


TOKENS: dict[str, tuple[str, ActorRole]] = {
    "operator-token-1": ("ops-1", ActorRole.OPERATOR),
    "owner-token-1": ("export-owner", ActorRole.OWNER),
    "owner-token-2": ("unrouted-owner", ActorRole.OWNER),
    "reporter-token-1": ("reporter-1", ActorRole.REPORTER),
    "reporter-token-2": ("mina-k", ActorRole.REPORTER),
    "agent-token-1": ("devin", ActorRole.AGENT),
}


def bearer(role: ActorRole, login: str | None = None) -> dict[str, str]:
    token = next(
        tok for tok, (lg, r) in TOKENS.items() if r == role and (login is None or lg == login)
    )
    return {"Authorization": f"Bearer {token}"}


def token_auth() -> AuthConfig:
    return AuthConfig(
        authenticator=StaticTokenAuthenticator(
            {
                tok: Principal(login=login, role=role, source="bearer")
                for tok, (login, role) in TOKENS.items()
            }
        )
    )


@pytest.fixture
def app(clock: FakeClock) -> FastAPI:
    return create_app(
        database_url="sqlite:///:memory:", seed_scenarios=True, clock=clock, auth=token_auth()
    )


@pytest.fixture
def unauthenticated_app(clock: FakeClock) -> FastAPI:
    return create_app(database_url="sqlite:///:memory:", seed_scenarios=True, clock=clock)


@pytest.fixture
def demo_app(clock: FakeClock) -> FastAPI:
    return create_app(
        database_url="sqlite:///:memory:",
        seed_scenarios=True,
        clock=clock,
        auth=AuthConfig(demo_principal=Principal("demo-operator", ActorRole.OPERATOR, "demo")),
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def unauthenticated_client(unauthenticated_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(unauthenticated_app) as c:
        yield c


@pytest.fixture
def demo_client(demo_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(demo_app) as c:
        yield c
