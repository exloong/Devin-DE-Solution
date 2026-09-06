"""Dashboard aggregation: throughput, session health, and liveness."""

from __future__ import annotations

from app.api import dashboard
from app.api.schemas import DashboardSummary
from app.domain.states import IssueState, SessionKind, SessionState
from app.persistence import runtime_status
from app.persistence.runtime_status import StatusRow

from tests.conftest import Harness


def _rows(h: Harness) -> dict[str, StatusRow]:
    with h.engine.connect() as conn:
        return runtime_status.read_all(conn)


def _by_kind(summary: DashboardSummary, kind: str) -> dict[str, object]:
    return next(s.model_dump() for s in summary.sessions if s.kind == kind)


def test_reproduction_is_auto_launched_and_fix_waits_for_owner(h: Harness) -> None:
    issue = h.classify(h.open_issue())
    assert issue.state == IssueState.REPRODUCING
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    repro = _by_kind(summary, "reproduction")
    fix = _by_kind(summary, "fix")
    assert repro["running"] == 1 and repro["total"] == 1
    assert fix["total"] == 0
    assert summary.heartbeat.last_session_launched_at == h.clock.now()
    assert [s.kind for s in summary.recent_sessions] == ["reproduction"]

    h.clock.advance(minutes=10)
    issue = h.reproduce(issue)
    assert issue.state == IssueState.NEEDS_OWNER_DECISION
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    assert _by_kind(summary, "fix")["total"] == 0

    h.clock.advance(minutes=5)
    issue = h.confirm(issue)
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    fix = _by_kind(summary, "fix")
    assert fix["queued"] == 1 and fix["total"] == 1
    assert summary.heartbeat.last_session_launched_at == h.clock.now()

    h.start_fix(issue)
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    assert _by_kind(summary, "fix")["running"] == 1
    assert [s.kind for s in summary.recent_sessions] == ["fix", "reproduction"]


def test_success_rate_and_median_duration_are_derived_from_sessions(h: Harness) -> None:
    first = h.classify(h.open_issue(1))
    h.clock.advance(minutes=10)
    h.reproduce(first)
    second = h.classify(h.open_issue(2))
    h.clock.advance(minutes=30)
    h.reproduce(second)
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    repro = _by_kind(summary, "reproduction")
    assert repro["completed"] == 2 and repro["failed"] == 0
    assert repro["success_rate_pct"] == 100.0
    assert repro["median_duration_seconds"] == 1200.0
    durations = sorted(s.duration_seconds for s in summary.recent_sessions)
    assert durations == [600.0, 1800.0]
    with h.uow() as uow:
        sessions = uow.list_sessions()
        assert all(s.kind == SessionKind.REPRODUCTION for s in sessions)
        assert all(s.state == SessionState.COMPLETED for s in sessions)


def test_throughput_and_in_flight_counts_come_from_issue_records(h: Harness) -> None:
    h.open_issue(1)
    h.classify(h.open_issue(2))
    h.classify(h.open_issue(3), missing=[{"field": "steps", "prompt": "Steps to reproduce?"}])
    with h.uow() as uow:
        summary = dashboard.summary(uow, h.clock.now(), _rows(h))
    assert summary.throughput.entered == 3
    assert summary.throughput.completed == 0
    today = summary.throughput.buckets[-1]
    assert today.day.date() == h.clock.now().date()
    assert today.entered == 3 and today.completed == 0
    stages = {stage.id: stage.count for stage in summary.in_flight}
    assert stages == {
        "intake": 1,
        "clarify": 1,
        "reproduce": 1,
        "confirm": 0,
        "fix": 0,
        "review": 0,
        "done": 0,
    }


def test_heartbeat_derives_overall_status(h: Harness) -> None:
    now = h.clock.now()
    with h.uow() as uow:
        summary = dashboard.summary(uow, now, {})
    hb = summary.heartbeat
    assert hb.overall == "down"
    assert hb.worker == "unavailable"
    assert hb.github == "unconfigured" and hb.devin == "unconfigured"
    assert hb.last_webhook_received_at is None

    runtime_status.touch_all(
        h.engine,
        {
            runtime_status.WORKER: "worker-1",
            runtime_status.PROVIDER_GITHUB: "dry_run",
            runtime_status.PROVIDER_DEVIN: "dry_run",
        },
        now,
    )
    with h.uow() as uow:
        hb = dashboard.summary(uow, now, _rows(h)).heartbeat
    assert hb.overall == "degraded"
    assert hb.worker == "ok"
    assert hb.github == "dry_run" and hb.devin == "dry_run"
    assert hb.intake == "none"
    # A missing Relay webhook is not an outage: reproduction intake is Devin-native.
    assert not any("webhook" in reason for reason in hb.reasons)

    with h.engine.begin() as conn:
        runtime_status.touch(conn, runtime_status.GITHUB_WEBHOOK, "issues", now)
    runtime_status.touch_all(
        h.engine,
        {runtime_status.PROVIDER_GITHUB: "live", runtime_status.PROVIDER_DEVIN: "live"},
        now,
    )
    with h.uow() as uow:
        hb = dashboard.summary(uow, now, _rows(h)).heartbeat
    assert hb.overall == "healthy"
    assert hb.github == "connected" and hb.devin == "connected"
    assert hb.last_webhook_received_at == now and hb.last_webhook_event == "issues"
    assert hb.intake == "webhook"
    assert hb.reasons == []

    with h.engine.begin() as conn:
        runtime_status.touch(conn, runtime_status.DEVIN_AUTOMATION_POLL, "auto-1", now)
    with h.uow() as uow:
        hb = dashboard.summary(uow, now, _rows(h)).heartbeat
    assert hb.intake == "native"
    assert hb.polled_automation_id == "auto-1"
    assert hb.last_automation_poll_at == now

    h.clock.advance(seconds=31)
    with h.uow() as uow:
        hb = dashboard.summary(uow, h.clock.now(), _rows(h)).heartbeat
    assert hb.worker == "stale"
    assert hb.github == "stale" and hb.devin == "stale"
    assert hb.intake == "stale"
    assert hb.overall == "degraded"
    assert "Devin automation intake poll is stale" in hb.reasons
