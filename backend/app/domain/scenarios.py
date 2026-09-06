"""Deterministic seeded scenarios.

Each scenario is a script of typed events replayed through the transition
service. Delivery IDs are stable, so seeding twice is idempotent. The issues
mirror the Relay mockup so the dashboard has recognizable data.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.domain.errors import DomainError, ErrorCode
from app.domain.models import Actor, Event, Issue, JobKind, JobStatus
from app.domain.ports import Clock, UnitOfWorkFactory
from app.domain.states import TARGET_REPOSITORY, ActorRole, EventType
from app.domain.transitions import TransitionService

SEED_SOURCE = "seed"


@dataclass(frozen=True)
class Step:
    type: EventType
    role: ActorRole
    payload: dict[str, object]
    login: str = "relay-system"
    revision: int | None = None
    # Simulated time elapsed before this step; timers are only legal once due.
    advance: timedelta = timedelta(0)


@dataclass(frozen=True)
class Scenario:
    name: str
    number: int
    steps: Sequence[Step] = field(default_factory=tuple)


def _opened(
    number: int,
    title: str,
    body: str,
    reporter: str,
    category: str,
    owner_team: str,
    labels: Iterable[str] = (),
) -> Step:
    return Step(
        EventType.ISSUE_OPENED,
        ActorRole.SYSTEM,
        {
            "repository": TARGET_REPOSITORY,
            "number": number,
            "title": title,
            "body": body,
            "reporter": reporter,
            "category": category,
            "owner_team": owner_team,
            "labels": list(labels),
            "target_commit": f"{number:07x}deadbeef",
            "superset_version": "6.1.0",
        },
    )


def _classified(missing: list[dict[str, str]] | None = None, **extra: object) -> Step:
    payload: dict[str, object] = {"missing_fields": missing or [], "confidence": 0.9}
    payload.update(extra)
    return Step(EventType.CLASSIFICATION_RESULT, ActorRole.AGENT, payload, login="devin-triage")


SESSION_PLACEHOLDER = "$SESSION"
TIMER_PLACEHOLDER = "$TIMER"
FIX_PR = {"number": 43302, "head_sha": "a1b2c3d4e5f6"}


def _timer(kind: EventType, advance: timedelta) -> Step:
    return Step(kind, ActorRole.SYSTEM, {"due_at": TIMER_PLACEHOLDER}, advance=advance)


class _OffsetClock:
    def __init__(self, base: Clock, offset: timedelta) -> None:
        self._base = base
        self._offset = offset

    def now(self) -> datetime:
        return self._base.now() + self._offset


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "awaiting_reporter",
        43218,
        (
            _opened(
                43218,
                "Dashboard filters reset after force refresh",
                "Native filters reset to defaults when the dashboard is force refreshed.",
                "mina-k",
                "Dashboard",
                "Dashboard Experience",
            ),
            _classified(
                [
                    {
                        "field": "screen_recording",
                        "prompt": "Attach a short recording of the filter reset.",
                        "why_it_matters": "Distinguishes a URL-state bug from a chart cache bug.",
                        "safe_example": "A recording using the example dashboards.",
                    },
                    {
                        "field": "feature_flags",
                        "prompt": "List the enabled DASHBOARD_* feature flags.",
                        "why_it_matters": "Filter persistence differs by flag.",
                        "safe_example": "DASHBOARD_NATIVE_FILTERS=true",
                    },
                ]
            ),
        ),
    ),
    Scenario(
        "reproducing",
        43207,
        (
            _opened(
                43207,
                "Trino temporal column displays shifted timezone",
                "Timestamps from Trino render shifted by the server offset.",
                "owen-l",
                "Databases",
                "Database Connectivity",
            ),
            _classified(),
            Step(
                EventType.SESSION_PROGRESS,
                ActorRole.AGENT,
                {
                    "session_id": SESSION_PLACEHOLDER,
                    "progress_percent": 64,
                    "current_action": "Comparing Trino timestamp fixtures: target vs control",
                    "next_checkpoint": "Attach minimal fixture and result matrix",
                    "label": "Target run",
                    "message": "Timezone shift reproduced twice on Superset 6.1.0.",
                },
                login="devin-repro",
            ),
        ),
    ),
    Scenario(
        "needs_owner_decision",
        43231,
        (
            _opened(
                43231,
                "Bulk tag removal returns a 500 response",
                "Removing more than 20 tags at once fails with an internal error.",
                "ari-p",
                "Backend",
                "Core Platform",
            ),
            _classified(),
            Step(
                EventType.REPRODUCTION_RESULT,
                ActorRole.AGENT,
                {
                    "session_id": SESSION_PLACEHOLDER,
                    "reproduced": False,
                    "summary": "No 500 on target or control with 25 tags; owner input needed.",
                    "evidence": [
                        {"kind": "reproduction_plan", "title": "Reproduction plan"},
                        {"kind": "fixture_manifest", "title": "Fixture manifest"},
                        {"kind": "result_matrix", "title": "Result matrix"},
                    ],
                },
                login="devin-repro",
                revision=1,
            ),
        ),
    ),
    Scenario(
        "awaiting_owner",
        42991,
        (
            _opened(
                42991,
                "CSV export ignores configured row limit",
                "CSV export returns every row regardless of the configured limit.",
                "jo-f",
                "Data export",
                "Core Platform",
            ),
            _classified(),
            Step(
                EventType.REPRODUCTION_RESULT,
                ActorRole.AGENT,
                {
                    "session_id": SESSION_PLACEHOLDER,
                    "reproduced": True,
                    "evidence": [{"kind": "result_matrix", "title": "Result matrix"}],
                },
                login="devin-repro",
                revision=1,
            ),
            Step(EventType.FIX_SESSION_STARTED, ActorRole.SYSTEM, {}),
            Step(
                EventType.FIX_RESULT,
                ActorRole.AGENT,
                {
                    "session_id": SESSION_PLACEHOLDER,
                    "pull_request": {
                        "repository": TARGET_REPOSITORY,
                        "number": 43302,
                        "head_branch": "devin/42991-csv-row-limit",
                        "head_sha": FIX_PR["head_sha"],
                        "url": f"https://github.com/{TARGET_REPOSITORY}/pull/43302",
                    },
                },
                login="devin-fix",
                revision=1,
            ),
            Step(
                EventType.REVIEWER_ROUTING_RESOLVED,
                ActorRole.SYSTEM,
                {
                    "pr_number": FIX_PR["number"],
                    "head_sha": FIX_PR["head_sha"],
                    "state": "resolved",
                    "candidates": [
                        {
                            "team": "core-platform",
                            "members": ["core-platform-lead", "export-owner"],
                            "rule": "superset/commands/export/** @core-platform",
                            "paths": ["superset/commands/export/csv.py"],
                            "rationale": "All changed paths match one CODEOWNERS rule.",
                        }
                    ],
                    "unowned_paths": [],
                    "ambiguous_paths": [],
                },
                login="codeowners-adapter",
            ),
            Step(
                EventType.DEVIN_REVIEW_COMPLETED,
                ActorRole.AGENT,
                {
                    "pr_number": FIX_PR["number"],
                    "head_sha": FIX_PR["head_sha"],
                    "verdict": "passed",
                    "findings": 0,
                },
                login="devin-review",
            ),
        ),
    ),
    Scenario(
        "redirected",
        43104,
        (
            _opened(
                43104,
                "Custom OAuth callback fails behind proxy",
                "Callback URL uses http when the deployment sits behind a TLS proxy.",
                "nora-s",
                "Configuration",
                "Security & Auth",
            ),
            Step(
                EventType.OWNER_DECISION,
                ActorRole.OWNER,
                {
                    "decision": "reclassify",
                    "reclassify_to": "unsupported",
                    "rationale": "Proxy header configuration; guidance delivered.",
                },
                login="auth-owner",
            ),
        ),
    ),
    Scenario(
        "closed_inactive",
        42856,
        (
            _opened(
                42856,
                "SQL Lab result disappears intermittently",
                "Results vanish after a few seconds on some queries.",
                "ilya-r",
                "SQL Lab",
                "SQL Lab",
            ),
            _classified(
                [
                    {"field": "worker_logs", "prompt": "Share redacted Celery worker logs."},
                    {"field": "reliable_trigger", "prompt": "Describe a query that triggers it."},
                ]
            ),
            _timer(EventType.REMINDER_ELAPSED, timedelta(days=3)),
            _timer(EventType.REMINDER_ELAPSED, timedelta(days=3)),
            _timer(EventType.INACTIVITY_ELAPSED, timedelta(days=8)),
        ),
    ),
    Scenario(
        "security_private",
        43240,
        (
            _opened(
                43240,
                "Possible XSS in markdown component",
                "A crafted markdown payload appears to execute script.",
                "sec-reporter",
                "Frontend",
                "Security & Auth",
            ),
        ),
    ),
)


@dataclass
class SeedResult:
    issues: dict[str, uuid.UUID] = field(default_factory=dict)
    applied: int = 0
    duplicates: int = 0
    rejected: int = 0


SCENARIO_NAMES: tuple[str, ...] = tuple(s.name for s in SCENARIOS)


def seed(
    uow_factory: UnitOfWorkFactory,
    service: TransitionService,
    *,
    only: str | None = None,
    settle_jobs: bool = True,
) -> SeedResult:
    result = SeedResult()
    with uow_factory() as uow:
        existing_job_ids = {job.id for job in uow.list_jobs()}
    selected = [s for s in SCENARIOS if only is None or s.name == only]
    if only is not None and not selected:
        raise DomainError(
            ErrorCode.INVALID_INPUT, "unknown scenario", {"available": list(SCENARIO_NAMES)}
        )
    base_clock = service.clock
    for scenario in selected:
        issue = _find_issue(uow_factory, scenario.number)
        offset = timedelta(0)
        for index, step in enumerate(scenario.steps):
            offset += step.advance
            service.clock = _OffsetClock(base_clock, offset)
            payload = dict(step.payload)
            if payload.get("session_id") == SESSION_PLACEHOLDER:
                if issue is None:
                    raise RuntimeError("scenario references a session before its issue exists")
                payload["session_id"] = _latest_session_id(uow_factory, issue.id)
            if payload.get("due_at") == TIMER_PLACEHOLDER:
                if issue is None:
                    raise RuntimeError("scenario references a timer before its issue exists")
                payload.update(_timer_payload(uow_factory, issue.id, step.type))
            event = Event(
                issue_id=issue.id if issue else None,
                source=SEED_SOURCE,
                delivery_id=f"{scenario.name}:{index}",
                type=step.type,
                actor=Actor(role=step.role, login=step.login),
                issue_revision=step.revision,
                payload=payload,
            )
            with uow_factory() as uow:
                try:
                    outcome = service.apply(uow, event)
                except DomainError:
                    result.rejected += 1
                    continue
                if outcome.duplicate:
                    result.duplicates += 1
                else:
                    result.applied += 1
                if outcome.issue is not None:
                    issue = outcome.issue
        if issue is not None:
            result.issues[scenario.name] = issue.id
    service.clock = base_clock
    if settle_jobs:
        with uow_factory() as uow:
            for job in uow.list_jobs():
                if job.id not in existing_job_ids and job.status in {
                    JobStatus.PENDING,
                    JobStatus.CLAIMED,
                }:
                    job.status = JobStatus.CANCELLED
                    job.finished_at = base_clock.now()
                    job.claimed_by = None
                    job.claimed_at = None
                    uow.save_job(job)
            uow.commit()
    return result


def _timer_payload(
    uow_factory: UnitOfWorkFactory, issue_id: uuid.UUID, kind: EventType
) -> dict[str, object]:
    with uow_factory() as uow:
        issue = uow.get_issue(issue_id)
    policy = issue.reporter_wait if issue else None
    if policy is None:
        return {"policy_revision": 0, "due_at": "1970-01-01T00:00:00+00:00"}
    due = policy.reminder_due_at if kind == EventType.REMINDER_ELAPSED else policy.inactivity_due_at
    timer_kind = JobKind.REMINDER if kind == EventType.REMINDER_ELAPSED else JobKind.INACTIVITY
    return {
        "policy_revision": policy.policy_revision,
        "due_at": due.isoformat() if due else "1970-01-01T00:00:00+00:00",
        "timer": timer_kind.value,
    }


def _find_issue(uow_factory: UnitOfWorkFactory, number: int) -> Issue | None:
    with uow_factory() as uow:
        repo = uow.get_repository()
        if repo is None:
            return None
        return uow.get_issue_by_number(repo.id, number)


def _latest_session_id(uow_factory: UnitOfWorkFactory, issue_id: uuid.UUID) -> str:
    with uow_factory() as uow:
        sessions = uow.list_sessions(issue_id=issue_id)
    if not sessions:
        raise RuntimeError("scenario expected a session")
    return str(sessions[-1].id)
