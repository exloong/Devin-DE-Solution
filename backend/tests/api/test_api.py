from __future__ import annotations

from typing import Any

from app.domain.scenarios import SCENARIO_NAMES
from app.domain.states import TARGET_REPOSITORY
from fastapi.testclient import TestClient

API = "/api/v1"


def _issue_by_key(client: TestClient, key: str) -> dict[str, Any]:
    items = client.get(f"{API}/issues").json()["items"]
    match = [i for i in items if i["key"] == key]
    assert match, [i["key"] for i in items]
    issue: dict[str, object] = match[0]
    return issue


def test_health_and_ready(client: TestClient) -> None:
    health = client.get(f"{API}/health")
    assert health.status_code == 200
    assert health.json()["repository"] == TARGET_REPOSITORY
    ready = client.get(f"{API}/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "database": "ok",
        "migrations": "0001",
        "dry_run": True,
    }


def test_seeded_issues_match_scenarios(client: TestClient) -> None:
    body = client.get(f"{API}/issues").json()
    assert body["total"] == len(SCENARIO_NAMES)
    states = {i["key"]: i["state"] for i in body["items"]}
    assert states == {
        "SUP-43218": "awaiting_reporter",
        "SUP-43207": "reproducing",
        "SUP-43231": "needs_owner_decision",
        "SUP-42991": "awaiting_owner",
        "SUP-43104": "unsupported",
        "SUP-42856": "closed_inactive",
        "SUP-43240": "security_private",
    }
    assert all(i["repository"] == TARGET_REPOSITORY for i in body["items"])
    live = [i["key"] for i in body["items"] if i["workspace_live"]]
    assert live == ["SUP-43207"]
    filtered = client.get(f"{API}/issues", params={"state": "awaiting_reporter"}).json()
    assert [i["key"] for i in filtered["items"]] == ["SUP-43218"]
    assert client.get(f"{API}/issues", params={"state": "bogus"}).status_code == 422


def test_issue_detail_is_redacted_and_typed(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-42991")
    detail = client.get(f"{API}/issues/{issue['id']}").json()
    assert "body" not in detail and "body" not in detail["issue"]
    assert detail["target_commit"] is not None
    assert [d["kind"] for d in detail["decisions"]] == ["confirm_bug"]
    pr = detail["pull_requests"][0]
    assert pr["repository"] == TARGET_REPOSITORY
    assert pr["devin_review"] == "passed"
    assert pr["human_approved"] is False
    assert all(not s["workspace_live"] for s in detail["sessions"])
    assert all(a["status"] == "applied" for a in detail["attempts"][:-1])
    missing = client.get(f"{API}/issues/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_sessions_and_session_detail(client: TestClient) -> None:
    sessions = client.get(f"{API}/sessions").json()
    assert sessions["total"] == 4
    running = client.get(f"{API}/sessions", params={"state": "running"}).json()["items"]
    assert len(running) == 1 and running[0]["issue_key"] == "SUP-43207"
    detail = client.get(f"{API}/sessions/{running[0]['id']}").json()
    assert detail["session"]["workspace_live"] is True
    assert detail["session"]["progress_percent"] == 64
    assert [m["author"] for m in detail["conversation"]] == ["devin"]
    assert detail["budget"]["wall_clock_seconds"] == 3600
    assert [e["label"] for e in detail["timeline"]] == ["Session queued", "Target run"]


def test_workflow_and_analytics(client: TestClient) -> None:
    workflow = client.get(f"{API}/workflow").json()
    assert workflow["repository"] == TARGET_REPOSITORY
    names = {t["name"] for t in workflow["transitions"]}
    assert {"intake", "confirm_bug", "route_security_private", "complete", "retry"} <= names
    confirm = next(t for t in workflow["transitions"] if t["name"] == "confirm_bug")
    assert confirm["human_gate"] is True and confirm["actors"] == ["owner"]
    summary = client.get(f"{API}/analytics/summary").json()
    assert summary["issues_total"] == 7
    assert summary["live_workspaces"] == 1
    assert summary["security_private"] == 1
    assert summary["attempts_rejected"] == 0


def test_dry_run_is_idempotent_and_never_executes_text(client: TestClient) -> None:
    payload = {
        "idempotency_key": "dry-1",
        "title": "Export hangs",
        "body": "$(curl evil) `rm -rf /` <script>alert(1)</script>",
        "labels": ["dashboard"],
    }
    first = client.post(f"{API}/dry-runs", json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["applied"] == 1
    issue = first.json()["issue"]
    assert issue["state"] == "triage" and issue["repository"] == TARGET_REPOSITORY
    second = client.post(f"{API}/dry-runs", json=payload)
    assert second.json()["duplicates"] == 1 and second.json()["issue"]["id"] == issue["id"]
    assert client.get(f"{API}/issues").json()["total"] == 8


def test_dry_run_security_text_fails_closed(client: TestClient) -> None:
    resp = client.post(
        f"{API}/dry-runs",
        json={"idempotency_key": "dry-sec", "title": "SQL injection in filter", "body": "..."},
    )
    assert resp.status_code == 201
    issue = resp.json()["issue"]
    assert issue["state"] == "security_private" and issue["security_flagged"] is True
    detail = client.get(f"{API}/issues/{issue['id']}").json()
    assert [j["kind"] for j in detail["jobs"]] == ["private_security_task"]


def test_dry_run_scenario_replay_is_idempotent(client: TestClient) -> None:
    resp = client.post(
        f"{API}/dry-runs", json={"idempotency_key": "x", "scenario": "needs_owner_decision"}
    )
    assert resp.status_code == 201
    assert resp.json()["applied"] == 0 and resp.json()["duplicates"] == 3
    bad = client.post(f"{API}/dry-runs", json={"idempotency_key": "y", "scenario": "nope"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"


def test_reporter_response_flow(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43218")
    partial = client.post(
        f"{API}/issues/{issue['id']}/reporter-responses",
        json={
            "idempotency_key": "rr-1",
            "answers": [{"field": "screen_recording", "unavailable": True}],
        },
    )
    assert partial.status_code == 200
    assert partial.json()["to_state"] == "awaiting_reporter"
    assert partial.json()["issue"]["open_questions"] == 1
    complete = client.post(
        f"{API}/issues/{issue['id']}/reporter-responses",
        json={
            "idempotency_key": "rr-2",
            "answers": [
                {"field": "screen_recording", "answer": "https://example.test/rec"},
                {"field": "feature_flags", "answer": "DASHBOARD_NATIVE_FILTERS=true"},
            ],
        },
    )
    assert complete.json()["to_state"] == "reproducing"
    assert complete.json()["issue"]["workspace_live"] is True
    bad = client.post(
        f"{API}/issues/{issue['id']}/reporter-responses",
        json={"idempotency_key": "rr-3", "answers": [{"field": "nope", "answer": "x"}]},
    )
    assert bad.status_code == 422


def test_owner_decision_gates_and_version_conflict(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43231")
    conflict = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"idempotency_key": "od-0", "decision": "confirm_bug", "expected_version": 99},
    )
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "version_conflict"
    agent = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"idempotency_key": "od-1", "decision": "confirm_bug", "actor_role": "agent"},
    )
    assert agent.status_code == 403
    assert agent.json()["error"]["code"] == "human_gate_required"
    ok = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={
            "idempotency_key": "od-2",
            "decision": "confirm_bug",
            "rationale": "Confirmed.",
            "expected_version": issue["version"],
        },
    )
    assert ok.status_code == 200 and ok.json()["to_state"] == "fix_authorized"
    replay = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"idempotency_key": "od-2", "decision": "confirm_bug"},
    )
    assert replay.json()["duplicate"] is True
    version = client.get(f"{API}/issues/{issue['id']}").json()["issue"]["version"]
    assert version == issue["version"] + 1


def test_devin_review_does_not_unlock_merge_via_api(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-42991")
    approve = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"idempotency_key": "ap-1", "decision": "approve_pr", "actor_login": "lead"},
    )
    assert approve.status_code == 200 and approve.json()["to_state"] == "awaiting_owner"
    pr = client.get(f"{API}/issues/{issue['id']}").json()["pull_requests"][0]
    assert pr["human_approved"] is True and pr["human_approver"] == "lead"


def test_retry_requires_recoverable_state(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43207")
    resp = client.post(f"{API}/issues/{issue['id']}/retry", json={"idempotency_key": "rt-1"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "illegal_transition"


def test_session_messages_and_cancel(client: TestClient) -> None:
    running = client.get(f"{API}/sessions", params={"state": "running"}).json()["items"][0]
    msg = client.post(
        f"{API}/sessions/{running['id']}/messages",
        json={"idempotency_key": "m-1", "body": "Please attach the fixture manifest."},
    )
    assert msg.status_code == 200 and msg.json()["status"] == "noop"
    detail = client.get(f"{API}/sessions/{running['id']}").json()
    assert detail["conversation"][-1]["author"] == "operator"
    cancel = client.post(
        f"{API}/sessions/{running['id']}/cancel-requests", json={"idempotency_key": "c-1"}
    )
    assert cancel.status_code == 200
    assert client.get(f"{API}/sessions/{running['id']}").json()["session"]["cancel_requested"]
    finished = client.get(f"{API}/sessions", params={"state": "completed"}).json()["items"][0]
    late = client.post(
        f"{API}/sessions/{finished['id']}/messages",
        json={"idempotency_key": "m-2", "body": "too late"},
    )
    assert late.status_code == 409
    missing = client.post(
        f"{API}/sessions/00000000-0000-0000-0000-000000000000/messages",
        json={"idempotency_key": "m-3", "body": "x"},
    )
    assert missing.status_code == 404


def test_validation_errors_use_error_envelope(client: TestClient) -> None:
    resp = client.post(f"{API}/dry-runs", json={"title": "no key"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_input"
