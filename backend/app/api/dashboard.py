"""System-health and throughput aggregation for the Overview dashboard.

Every number is derived from persisted issue/session records and the
``runtime_status`` liveness rows. Nothing here is estimated.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from statistics import median

from app.api.queries import _WORKFLOW_STEPS
from app.api.schemas import (
    AutomationStatus,
    DashboardSummary,
    Heartbeat,
    IntakeStatus,
    Period,
    ProbeStatus,
    ProviderStatus,
    RecentSession,
    SessionHealth,
    StageCount,
    SystemStatus,
    Throughput,
    ThroughputBucket,
)
from app.domain.models import AgentSession, Issue
from app.domain.ports import UnitOfWork
from app.domain.states import TERMINAL_STATES, SessionKind, SessionState
from app.persistence import runtime_status
from app.persistence.automation_registry import AutomationRecord
from app.persistence.runtime_status import StatusRow

THROUGHPUT_DAYS = 14
RECENT_LIMIT = 8
WORKER_FRESH = timedelta(seconds=30)


def _day(value: datetime) -> datetime:
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def throughput(issues: list[Issue], now: datetime) -> Throughput:
    start = _day(now) - timedelta(days=THROUGHPUT_DAYS - 1)
    entered: Counter[datetime] = Counter()
    completed: Counter[datetime] = Counter()
    for issue in issues:
        entered[_day(issue.created_at)] += 1
        if issue.state in TERMINAL_STATES:
            completed[_day(issue.updated_at)] += 1
    buckets = [
        ThroughputBucket(
            day=start + timedelta(days=offset),
            entered=entered[start + timedelta(days=offset)],
            completed=completed[start + timedelta(days=offset)],
        )
        for offset in range(THROUGHPUT_DAYS)
    ]
    return Throughput(
        entered=len(issues),
        completed=sum(1 for issue in issues if issue.state in TERMINAL_STATES),
        buckets=buckets,
    )


def in_flight(issues: list[Issue]) -> list[StageCount]:
    counts = Counter(issue.state for issue in issues)
    return [
        StageCount(
            id=step_id,
            label=label,
            kind=kind,
            actor=actor,
            count=sum(counts[state] for state in states),
        )
        for step_id, label, kind, states, actor in _WORKFLOW_STEPS
    ]


def _duration(session: AgentSession) -> float | None:
    if session.started_at is None or session.finished_at is None:
        return None
    return (session.finished_at - session.started_at).total_seconds()


def session_health(sessions: list[AgentSession], kind: SessionKind) -> SessionHealth:
    own = [s for s in sessions if s.kind == kind]
    states = Counter(s.state for s in own)
    finished = states[SessionState.COMPLETED] + states[SessionState.FAILED]
    durations = [
        duration
        for s in own
        if s.state == SessionState.COMPLETED and (duration := _duration(s)) is not None
    ]
    return SessionHealth(
        kind=kind.value,
        queued=states[SessionState.QUEUED],
        running=states[SessionState.RUNNING],
        completed=states[SessionState.COMPLETED],
        failed=states[SessionState.FAILED],
        needs_attention=states[SessionState.NEEDS_ATTENTION],
        cancelled=states[SessionState.CANCELLED],
        total=len(own),
        success_rate_pct=(
            round(100 * states[SessionState.COMPLETED] / finished, 1) if finished else None
        ),
        median_duration_seconds=round(median(durations), 1) if durations else None,
        last_launched_at=max((s.created_at for s in own), default=None),
    )


def recent_sessions(
    sessions: list[AgentSession], issues: Mapping[uuid.UUID, Issue]
) -> list[RecentSession]:
    out: list[RecentSession] = []
    for session in sorted(sessions, key=lambda s: s.created_at, reverse=True):
        issue = issues.get(session.issue_id)
        if issue is None:
            continue
        out.append(
            RecentSession(
                id=session.id,
                kind=session.kind.value,
                issue_id=issue.id,
                issue_key=issue.key,
                issue_title=issue.title,
                status=session.state.value,
                dry_run=session.dry_run,
                created_at=session.created_at,
                started_at=session.started_at,
                ended_at=session.finished_at,
                duration_seconds=_duration(session),
                devin_session_url=session.external_session_url,
            )
        )
        if len(out) >= RECENT_LIMIT:
            break
    return out


def _probe(row: StatusRow | None, now: datetime) -> ProbeStatus:
    if row is None:
        return "unavailable"
    return "ok" if now - row.updated_at <= WORKER_FRESH else "stale"


def _provider(row: StatusRow | None, worker: ProbeStatus) -> ProviderStatus:
    if row is None:
        return "unconfigured"
    if worker != "ok":
        return "stale"
    return "connected" if row.instance_id == "live" else "dry_run"


def heartbeat(
    rows: Mapping[str, StatusRow],
    sessions: list[AgentSession],
    now: datetime,
    *,
    database: ProbeStatus = "ok",
    automations: Sequence[AutomationRecord] = (),
) -> Heartbeat:
    worker_row = rows.get(runtime_status.WORKER)
    worker = _probe(worker_row, now)
    github = _provider(rows.get(runtime_status.PROVIDER_GITHUB), worker)
    devin = _provider(rows.get(runtime_status.PROVIDER_DEVIN), worker)
    webhook = rows.get(runtime_status.GITHUB_WEBHOOK)
    poll = rows.get(runtime_status.DEVIN_AUTOMATION_POLL)
    intake: IntakeStatus
    if poll is not None and now - poll.updated_at <= WORKER_FRESH:
        intake = "native"
    elif poll is not None:
        intake = "stale"
    elif webhook is not None:
        intake = "webhook"
    else:
        intake = "none"
    reasons: list[str] = []
    if database != "ok":
        reasons.append("database is not reachable")
    if worker == "unavailable":
        reasons.append("no worker heartbeat has been recorded")
    elif worker == "stale":
        reasons.append("worker heartbeat is stale")
    if github == "unconfigured":
        reasons.append("GitHub provider status has not been reported")
    elif github == "dry_run":
        reasons.append("GitHub runs in dry-run mode")
    if devin == "unconfigured":
        reasons.append("Devin provider status has not been reported")
    elif devin == "dry_run":
        reasons.append("Devin sessions run in dry-run mode")
    if intake == "stale":
        reasons.append("Devin automation intake poll is stale")
    elif intake == "none" and devin == "connected":
        reasons.append("no Devin automation poll or GitHub webhook has been observed")
    overall: SystemStatus
    if database != "ok" or worker == "unavailable":
        overall = "down"
    elif reasons:
        overall = "degraded"
    else:
        overall = "healthy"
    return Heartbeat(
        database=database,
        worker=worker,
        github=github,
        devin=devin,
        last_worker_heartbeat_at=worker_row.updated_at if worker_row else None,
        last_webhook_received_at=webhook.updated_at if webhook else None,
        last_webhook_event=webhook.instance_id if webhook else None,
        intake=intake,
        last_automation_poll_at=poll.updated_at if poll else None,
        polled_automation_id=poll.instance_id if poll else None,
        last_session_launched_at=max((s.created_at for s in sessions), default=None),
        automations=[
            AutomationStatus(
                kind=SessionKind(record.kind),
                automation_id=record.automation_id,
                enabled=record.enabled,
                updated_at=record.updated_at,
            )
            for record in automations
        ],
        overall=overall,
        reasons=reasons,
    )


def summary(
    uow: UnitOfWork,
    now: datetime,
    rows: Mapping[str, StatusRow],
    *,
    database: ProbeStatus = "ok",
    automations: Sequence[AutomationRecord] = (),
) -> DashboardSummary:
    issues = list(uow.list_issues())
    sessions = list(uow.list_sessions())
    by_id = {issue.id: issue for issue in issues}
    earliest = min((issue.created_at for issue in issues), default=now)
    return DashboardSummary(
        period=Period(from_=earliest, to=now),
        throughput=throughput(issues, now),
        in_flight=in_flight(issues),
        sessions=[
            session_health(sessions, SessionKind.REPRODUCTION),
            session_health(sessions, SessionKind.FIX),
        ],
        recent_sessions=recent_sessions(sessions, by_id),
        heartbeat=heartbeat(rows, sessions, now, database=database, automations=automations),
        generated_at=now,
    )
