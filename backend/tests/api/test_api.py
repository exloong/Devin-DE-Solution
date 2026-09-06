from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from app.api.app import auth_from_env
from app.domain.scenarios import SCENARIO_NAMES
from app.domain.states import TARGET_REPOSITORY, ActorRole
from app.persistence import tables
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from tests.conftest import FakeClock, bearer

API = "/api/v1"
OPERATOR = bearer(ActorRole.OPERATOR)
OWNER = bearer(ActorRole.OWNER, "export-owner")
UNROUTED_OWNER = bearer(ActorRole.OWNER, "unrouted-owner")
REPORTER = bearer(ActorRole.REPORTER)
ORIGINAL_REPORTER = bearer(ActorRole.REPORTER, "mina-k")
AGENT = bearer(ActorRole.AGENT)
WEBHOOK_SECRET = "test-webhook-secret"


def _cmd(key: str, version: object = None, **auth: str) -> dict[str, str]:
    headers = {"Idempotency-Key": key, **auth}
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    return headers


def _get(client: TestClient, url: str, params: dict[str, str] | None = None) -> Response:
    resp: Response = client.get(url, params=params, headers=OPERATOR)
    return resp


def _version(client: TestClient, issue_id: object) -> int:
    return int(str(_detail(client, issue_id).json()["version"]))


def _session_version(client: TestClient, session_id: object) -> int:
    return int(str(_get(client, f"{API}/sessions/{session_id}").json()["version"]))


def _issue_by_key(client: TestClient, key: str) -> Response:
    resp = _get(client, f"{API}/issues", params={"search": key})
    assert resp.status_code == 200 and resp.json()["total"] == 1, resp.text
    return resp


def _detail(client: TestClient, issue_id: object) -> Response:
    resp = _get(client, f"{API}/issues/{issue_id}")
    assert resp.status_code == 200, resp.text
    return resp


def _question_id(client: TestClient, issue_id: object, field: str) -> str:
    questions = _detail(client, issue_id).json()["questions"]
    assert isinstance(questions, list)
    return str(next(q["id"] for q in questions if q["field"] == field))


def _webhook(
    client: TestClient,
    payload: dict[str, object],
    delivery: str,
    *,
    event: str = "issues",
    valid_signature: bool = True,
) -> Response:
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    if not valid_signature:
        signature = "0" * 64
    return client.post(
        f"{API}/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


# ------------------------------------------------------------------- queries


def test_health_and_ready(client: TestClient) -> None:
    health = client.get(f"{API}/health")
    assert health.status_code == 200
    assert health.json()["repository"] == TARGET_REPOSITORY
    ready = client.get(f"{API}/ready")
    assert ready.status_code == 200
    assert ready.json() == {
        "database": "ok",
        "worker": "unavailable",
        "last_worker_heartbeat_at": None,
        "migrations": "0004",
        "dry_run": True,
    }


def test_ready_reports_fresh_and_stale_worker(
    app: FastAPI, client: TestClient, clock: FakeClock
) -> None:
    with app.state.context.engine.begin() as conn:
        conn.execute(
            tables.runtime_status.insert().values(
                component="worker",
                instance_id="test-worker",
                updated_at=clock.now(),
            )
        )
    assert client.get(f"{API}/ready").json()["worker"] == "ok"
    clock.advance(seconds=31)
    assert client.get(f"{API}/ready").json()["worker"] == "stale"


def test_github_webhook_is_signed_scoped_and_idempotent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload: dict[str, object] = {
        "action": "opened",
        "repository": {"full_name": TARGET_REPOSITORY},
        "issue": {
            "number": 999001,
            "title": "Webhook test",
            "body": "A reproducible failure.",
            "user": {"login": "reporter-1"},
            "labels": [],
        },
        "sender": {"login": "reporter-1"},
    }
    accepted = _webhook(client, payload, "webhook-accepted-1")
    duplicate = _webhook(client, payload, "webhook-accepted-1")
    assert accepted.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json() == accepted.json()

    wrong_repository = {**payload, "repository": {"full_name": "apache/superset"}}
    rejected = _webhook(client, wrong_repository, "webhook-wrong-repository")
    assert rejected.status_code == 403
    assert rejected.json()["error"]["code"] == "unauthorized_repository"

    unsigned = _webhook(client, payload, "webhook-invalid-signature", valid_signature=False)
    assert unsigned.status_code == 401
    assert unsigned.json()["error"]["code"] == "invalid_signature"


def test_github_webhook_records_heartbeat_and_gates_labels(
    client: TestClient, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    monkeypatch.setenv("RELAY_TRUSTED_LABEL", "relay:triage")
    monkeypatch.setenv("RELAY_TRUSTED_ACTORS", "maintainer")
    issue: dict[str, object] = {
        "number": 999101,
        "title": "Labeled intake",
        "body": "Fails on export.",
        "state": "open",
        "user": {"login": "reporter-1"},
        "labels": [],
    }
    base: dict[str, object] = {"repository": {"full_name": TARGET_REPOSITORY}, "issue": issue}

    before = _get(client, f"{API}/dashboard").json()["heartbeat"]
    assert before["last_webhook_received_at"] is None

    opened = _webhook(
        client, {**base, "action": "opened", "sender": {"login": "reporter-1"}}, "g-1"
    )
    assert opened.status_code == 202
    assert opened.json()["action"] == "ignored"
    assert opened.json()["reason"] == "untrusted_actor"

    labeled_wrong = _webhook(
        client,
        {**base, "action": "labeled", "label": {"name": "bug"}, "sender": {"login": "maintainer"}},
        "g-2",
    )
    assert labeled_wrong.json()["reason"] == "label_not_trusted"
    assert _get(client, f"{API}/issues", params={"search": "SUP-999101"}).json()["total"] == 0

    labeled = _webhook(
        client,
        {
            **base,
            "action": "labeled",
            "label": {"name": "relay:triage"},
            "sender": {"login": "maintainer"},
        },
        "g-3",
    )
    assert labeled.status_code == 202, labeled.text
    assert labeled.json()["state"] == "triage"
    replay = _webhook(
        client,
        {
            **base,
            "action": "labeled",
            "label": {"name": "relay:triage"},
            "sender": {"login": "maintainer"},
        },
        "g-4",
    )
    assert replay.json() == {
        "accepted": True,
        "delivery": "g-4",
        "action": "ignored",
        "reason": "issue_already_enrolled",
    }

    heartbeat = _get(client, f"{API}/dashboard").json()["heartbeat"]
    assert heartbeat["last_webhook_event"] == "issues"
    assert heartbeat["last_webhook_received_at"] is not None
    # An unsigned delivery never counts as a received webhook.
    clock.advance(minutes=1)
    unsigned = _webhook(client, {**base, "action": "opened"}, "g-5", valid_signature=False)
    assert unsigned.status_code == 401
    after = _get(client, f"{API}/dashboard").json()["heartbeat"]
    assert after["last_webhook_received_at"] == heartbeat["last_webhook_received_at"]


def test_dashboard_reports_seeded_sessions_and_stages(client: TestClient) -> None:
    resp = _get(client, f"{API}/dashboard")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["throughput"]["entered"] == len(SCENARIO_NAMES)
    assert sum(stage["count"] for stage in body["in_flight"]) == len(SCENARIO_NAMES)
    assert [s["kind"] for s in body["sessions"]] == ["reproduction", "fix"]
    assert body["heartbeat"]["overall"] == "down"
    for recent in body["recent_sessions"]:
        assert recent["kind"] in {"reproduction", "fix"}
        assert recent["issue_key"].startswith("SUP-")
    assert client.get(f"{API}/dashboard").status_code == 401


def test_github_webhook_accepts_ping_without_creating_an_issue(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    payload: dict[str, object] = {
        "repository": {"full_name": TARGET_REPOSITORY},
        "sender": {"login": "github"},
        "zen": "Keep it logically awesome.",
    }

    accepted = _webhook(client, payload, "webhook-ping-1", event="ping")

    assert accepted.status_code == 202
    assert accepted.json() == {
        "accepted": True,
        "delivery": "webhook-ping-1",
        "action": "ignored",
    }
    assert _get(client, f"{API}/issues").json()["total"] == len(SCENARIO_NAMES)


def test_github_webhook_secret_can_be_loaded_from_file(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "webhook-secret"
    secret_file.write_text(f"{WEBHOOK_SECRET}\n", encoding="utf-8")
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_FILE", str(secret_file))
    payload: dict[str, object] = {
        "action": "opened",
        "repository": {"full_name": TARGET_REPOSITORY},
        "issue": {
            "number": 999002,
            "title": "Webhook file-secret test",
            "body": "A reproducible failure.",
            "user": {"login": "reporter-1"},
            "labels": [],
        },
        "sender": {"login": "reporter-1"},
    }

    accepted = _webhook(client, payload, "webhook-file-secret")

    assert accepted.status_code == 202


def test_operator_auth_tokens_can_be_loaded_from_file(tmp_path: Path) -> None:
    token_file = tmp_path / "operator-tokens"
    token_file.write_text("test-token:relay-operator:operator\n", encoding="utf-8")

    auth = auth_from_env(
        {
            "RELAY_MODE": "live",
            "RELAY_AUTH_TOKENS_FILE": str(token_file),
        }
    )

    assert auth.enabled
    assert auth.demo_principal is None


def test_seeded_issues_match_scenarios(client: TestClient) -> None:
    body = _get(client, f"{API}/issues").json()
    assert body["total"] == len(SCENARIO_NAMES) and body["generated_at"]
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
    for item in body["items"]:
        assert item["repository"] == {
            "full_name": TARGET_REPOSITORY,
            "html_url": f"https://github.com/{TARGET_REPOSITORY}",
            "default_branch": "master",
            "dry_run": True,
        }
        assert item["html_url"].startswith(f"https://github.com/{TARGET_REPOSITORY}/issues/")
        assert item["reporter"]["kind"] == "reporter"
        assert item["human_gate"]["workspace_released"] is (not item["workspace_live"])
    live = [i["key"] for i in body["items"] if i["workspace_live"]]
    assert live == ["SUP-43207"]
    gates = {i["key"]: i["human_gate"]["kind"] for i in body["items"]}
    assert gates["SUP-43218"] == "reporter" and gates["SUP-42991"] == "owner"
    assert gates["SUP-43240"] == "security" and gates["SUP-43207"] == "none"
    filtered = _get(client, f"{API}/issues", params={"state": "awaiting_reporter"}).json()
    assert [i["key"] for i in filtered["items"]] == ["SUP-43218"]
    searched = _get(client, f"{API}/issues", params={"search": "sup-42991"}).json()
    assert [i["key"] for i in searched["items"]] == ["SUP-42991"]
    bad = _get(client, f"{API}/issues", params={"state": "bogus"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"


def test_issue_detail_is_flat_redacted_and_typed(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-42991").json()["items"][0]
    detail = _detail(client, issue["id"]).json()
    assert "body" not in detail and detail.get("body_excerpt") is None
    assert detail["state"] == "awaiting_owner" and detail["target_commit"] is not None
    assert [d["kind"] for d in detail["decisions"]] == ["confirm_bug"]
    decision = detail["decisions"][0]
    assert decision["actor"]["kind"] == "owner" and decision["authorization"]["scope"]
    pr = detail["pull_requests"][0]
    assert pr["repository"] == TARGET_REPOSITORY and pr["state"] == "open"
    assert pr["review"] == "requested" and pr["human_approver"] is None
    routing = detail["owner_routing"]
    assert routing["state"] == "resolved" and routing["source"] == ".github/CODEOWNERS"
    assert routing["candidates"][0]["team"] == "core-platform"
    assert routing["head_sha"] == pr["head_sha"]
    assert detail["evidence"]["reproduction_ready"] is True
    assert detail["evidence"]["security_classification"] == "none"
    assert all(e["outcome"] == "accepted" for e in detail["events"])
    assert detail["session_ids"] and detail["workspace_live"] is False
    missing = _get(client, f"{API}/issues/{uuid.UUID(int=0)}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_sessions_expose_only_synchronized_live_data(client: TestClient) -> None:
    sessions = _get(client, f"{API}/sessions").json()
    assert sessions["total"] == 4 and sessions["generated_at"]
    running = _get(client, f"{API}/sessions", params={"status": "running"}).json()["items"]
    assert len(running) == 1 and running[0]["issue_key"] == "SUP-43207"
    detail = _get(client, f"{API}/sessions/{running[0]['id']}").json()
    assert detail["workspace"]["released"] is False and detail["workspace"]["id"]
    assert detail["dry_run"] is True
    assert detail["progress"] == 64 and detail["progress_source"] == "seed:devin-repro"
    assert detail["progress_synced_at"] is not None
    assert detail["repository"]["full_name"] == TARGET_REPOSITORY
    assert detail["budget"]["wall_seconds"] == 3600
    assert [m["author"]["kind"] for m in detail["conversation"]] == ["agent"]
    assert [e["label"] for e in detail["events"]] == ["Session queued", "Target run"]
    assert detail["links"] == {
        "devin_session_url": None,
        "devin_desktop_url": None,
        "conversation_embeddable": False,
        "desktop_embeddable": False,
    }
    assert detail["review"]["status"] == "not_requested"
    completed = _get(client, f"{API}/sessions", params={"status": "completed"}).json()["items"]
    for s in completed:
        assert s["workspace"]["released"] is True and s["workspace"]["id"] is None
        assert s["workspace"]["released_at"] is not None
        assert s["current_action"] is None and s["next_checkpoint"] is None
    fix = next(s for s in completed if s["kind"] == "fix")
    fix_detail = _get(client, f"{API}/sessions/{fix['id']}").json()
    assert fix_detail["review"]["status"] == "completed"
    assert fix_detail["review"]["verdict"] == "passed"
    assert fix_detail["pull_requests"][0]["number"] == 43302


def test_workflow_and_analytics(client: TestClient) -> None:
    workflow = client.get(f"{API}/workflow").json()
    assert workflow["repository"] == TARGET_REPOSITORY and workflow["status"] == "active"
    assert [s["id"] for s in workflow["steps"]] == [
        "intake",
        "clarify",
        "reproduce",
        "confirm",
        "fix",
        "review",
        "done",
    ]
    names = {t["name"] for t in workflow["transitions"]}
    assert {"intake", "confirm_bug", "route_security_private", "complete", "retry"} <= names
    confirm = next(t for t in workflow["transitions"] if t["name"] == "confirm_bug")
    assert confirm["human_gate"] is True and confirm["actors"] == ["owner"]
    summary = _get(client, f"{API}/analytics/summary").json()
    assert summary["issues_processed"] == 7 and summary["confirmed_bugs"] == 1
    assert summary["live_workspaces"] == 1
    assert summary["security_private"] == 1
    assert summary["attempts_rejected"] == 0
    assert summary["state_counts"]["awaiting_owner"] == 1
    assert {o["label"] for o in summary["outcome_mix"]} == {"unsupported", "closed_inactive"}
    assert summary["period"]["from"] <= summary["period"]["to"] == summary["generated_at"]


# ---------------------------------------------------------------------- auth


def test_commands_denied_without_configured_authentication(
    unauthenticated_client: TestClient,
) -> None:
    resp = unauthenticated_client.post(f"{API}/dry-runs", json={"title": "x"}, headers=_cmd("k-1"))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized_actor"
    # Presenting a token to a deployment without auth configured is also denied.
    resp = unauthenticated_client.post(
        f"{API}/dry-runs", json={"title": "x"}, headers=_cmd("k-2", **OPERATOR)
    )
    assert resp.status_code == 401
    reads = unauthenticated_client.get(f"{API}/issues")
    assert reads.status_code == 401 and reads.json()["error"]["code"] == "unauthorized_actor"
    assert unauthenticated_client.get(f"{API}/health").status_code == 200
    assert unauthenticated_client.get(f"{API}/workflow").status_code == 200


def test_reads_require_operator_principal(client: TestClient) -> None:
    for path in ("/issues", "/sessions", "/analytics/summary"):
        anonymous = client.get(f"{API}{path}")
        assert anonymous.status_code == 401, path
        assert anonymous.json()["error"]["code"] == "unauthorized_actor"
        for headers in (REPORTER, OWNER, AGENT):
            denied = client.get(f"{API}{path}", headers=headers)
            assert denied.status_code == 403, (path, headers)
            assert denied.json()["error"]["code"] == "unauthorized_actor"
        assert client.get(f"{API}{path}", headers=OPERATOR).status_code == 200
    issue = _issue_by_key(client, "SUP-43240").json()["items"][0]
    for path in (f"/issues/{issue['id']}", f"/sessions/{uuid.UUID(int=0)}"):
        assert client.get(f"{API}{path}").status_code == 401
        assert client.get(f"{API}{path}", headers=REPORTER).status_code == 403
    assert client.get(f"{API}/health").status_code == 200
    assert client.get(f"{API}/workflow").status_code == 200


def test_demo_principal_is_explicit_and_server_side(demo_client: TestClient) -> None:
    resp = demo_client.post(f"{API}/dry-runs", json={"title": "Demo"}, headers=_cmd("demo-1"))
    assert resp.status_code == 201, resp.text
    issue = demo_client.get(f"{API}/issues/{resp.json()['resource_id']}").json()
    intake = next(e for e in issue["events"] if e["kind"] == "intake")
    assert intake["actor"]["kind"] == "system"
    assert intake["actor"]["login"] == "dry-run:demo-operator"


def test_missing_or_bad_credentials_are_rejected(client: TestClient) -> None:
    no_auth = client.post(f"{API}/dry-runs", json={"title": "x"}, headers=_cmd("a-1"))
    assert no_auth.status_code == 401 and no_auth.json()["error"]["code"] == "unauthorized_actor"
    bad = client.post(
        f"{API}/dry-runs",
        json={"title": "x"},
        headers=_cmd("a-2", Authorization="Bearer not-a-real-token"),
    )
    assert bad.status_code == 401
    basic = client.post(
        f"{API}/dry-runs", json={"title": "x"}, headers=_cmd("a-3", Authorization="Basic abc")
    )
    assert basic.status_code == 401


def test_actor_identity_cannot_be_forged_from_body(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43231").json()["items"][0]
    version = issue["version"]
    for forged in (
        {"kind": "confirm_bug", "actor_role": "owner"},
        {"kind": "confirm_bug", "actor_login": "export-owner"},
        {"kind": "confirm_bug", "idempotency_key": "in-body"},
    ):
        resp = client.post(
            f"{API}/issues/{issue['id']}/decisions",
            json=forged,
            headers=_cmd("f-1", version, **AGENT),
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["error"]["code"] == "invalid_input"
    as_agent = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"kind": "confirm_bug"},
        headers=_cmd("f-2", version, **AGENT),
    )
    assert as_agent.status_code == 403
    assert as_agent.json()["error"]["code"] == "human_gate_required"
    as_reporter = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"kind": "confirm_bug"},
        headers=_cmd("f-3", version, **REPORTER),
    )
    assert as_reporter.status_code == 403
    assert _detail(client, issue["id"]).json()["state"] == "needs_owner_decision"


def test_dry_runs_require_operator_principal(client: TestClient) -> None:
    resp = client.post(f"{API}/dry-runs", json={"title": "x"}, headers=_cmd("d-r", **REPORTER))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "unauthorized_actor"


# ------------------------------------------------------------------- headers


def test_idempotency_key_header_is_required(client: TestClient) -> None:
    resp = client.post(f"{API}/dry-runs", json={"title": "x"}, headers=OPERATOR)
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "invalid_input" and body["details"]["header"] == "Idempotency-Key"
    blank = client.post(
        f"{API}/dry-runs", json={"title": "x"}, headers={**OPERATOR, "Idempotency-Key": "  "}
    )
    assert blank.status_code == 422


def test_if_match_precondition(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43231").json()["items"][0]
    url = f"{API}/issues/{issue['id']}/decisions"
    missing = client.post(url, json={"kind": "confirm_bug"}, headers=_cmd("v-m", **OWNER))
    assert missing.status_code == 422, missing.text
    assert missing.json()["error"]["details"]["header"] == "If-Match"
    assert _detail(client, issue["id"]).json()["state"] == "needs_owner_decision"
    malformed = client.post(
        url, json={"kind": "confirm_bug"}, headers={**_cmd("v-0", **OWNER), "If-Match": "abc"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["error"]["details"]["header"] == "If-Match"
    conflict = client.post(url, json={"kind": "confirm_bug"}, headers=_cmd("v-1", 99, **OWNER))
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "version_conflict"
    version = int(str(issue["version"]))
    weak = client.post(
        url,
        json={"kind": "confirm_bug", "rationale": "Confirmed."},
        headers={**_cmd("v-2", **OWNER), "If-Match": f'W/"{version}"'},
    )
    assert weak.status_code == 200, weak.text
    accepted = weak.json()
    assert accepted["to_state"] == "fix_authorized" and accepted["processing"] == "complete"
    assert accepted["resource_id"] == issue["id"]
    assert accepted["resource_version"] == version + 1
    assert _detail(client, issue["id"]).json()["version"] == version + 1
    replay = client.post(
        url, json={"kind": "confirm_bug"}, headers=_cmd("v-2", version + 1, **OWNER)
    )
    assert replay.status_code == 200 and replay.json()["duplicate"] is True


# ------------------------------------------------------------------ commands


def test_dry_run_is_idempotent_and_never_executes_text(client: TestClient) -> None:
    payload = {
        "title": "Export hangs",
        "body": "$(curl evil) `rm -rf /` <script>alert(1)</script>",
        "labels": ["dashboard"],
    }
    first = client.post(f"{API}/dry-runs", json=payload, headers=_cmd("dry-1", **OPERATOR))
    assert first.status_code == 201, first.text
    accepted = first.json()
    assert accepted["duplicate"] is False and accepted["to_state"] == "triage"
    issue = _detail(client, accepted["resource_id"]).json()
    assert issue["repository"]["dry_run"] is True and issue["labels"] == ["dashboard"]
    second = client.post(f"{API}/dry-runs", json=payload, headers=_cmd("dry-1", **OPERATOR))
    assert second.json()["duplicate"] is True
    assert second.json()["resource_id"] == accepted["resource_id"]
    assert _get(client, f"{API}/issues").json()["total"] == 8


def test_dry_run_security_text_fails_closed(client: TestClient) -> None:
    resp = client.post(
        f"{API}/dry-runs",
        json={"title": "SQL injection in filter", "body": "..."},
        headers=_cmd("dry-sec", **OPERATOR),
    )
    assert resp.status_code == 201
    issue = _detail(client, resp.json()["resource_id"]).json()
    assert issue["state"] == "security_private" and issue["security_flagged"] is True
    assert issue["human_gate"]["kind"] == "security"
    assert issue["evidence"]["security_classification"] == "confirmed_private"
    assert issue["evidence"]["reproduction_ready"] is False


def test_dry_run_scenario_replay_is_idempotent(client: TestClient) -> None:
    resp = client.post(
        f"{API}/dry-runs", json={"scenario": "needs_owner_decision"}, headers=_cmd("x", **OPERATOR)
    )
    assert resp.status_code == 201
    assert resp.json()["duplicate"] is True and resp.json()["applied"] == 0
    bad = client.post(f"{API}/dry-runs", json={"scenario": "nope"}, headers=_cmd("y", **OPERATOR))
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_input"


def test_reporter_response_flow(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43218").json()["items"][0]
    url = f"{API}/issues/{issue['id']}/responses"
    recording = _question_id(client, issue["id"], "screen_recording")
    unavailable = {
        "issue_revision": issue["revision"] if "revision" in issue else 1,
        "question_id": recording,
        "response": {"kind": "unavailable", "reason": "cannot record"},
    }
    # Only the original reporter (mina-k) may answer; another reporter is refused.
    stranger = client.post(
        url, json=unavailable, headers=_cmd("rr-0", issue["version"], **REPORTER)
    )
    assert stranger.status_code == 403, stranger.text
    assert stranger.json()["error"]["code"] == "unauthorized_actor"
    # An operator may only answer on the reporter's behalf with an audited rationale.
    no_override = client.post(
        url, json=unavailable, headers=_cmd("rr-0b", issue["version"], **OPERATOR)
    )
    assert no_override.status_code == 403
    partial = client.post(
        url,
        json=unavailable,
        headers=_cmd("rr-1", issue["version"], **ORIGINAL_REPORTER),
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["to_state"] == "awaiting_reporter"
    assert _detail(client, issue["id"]).json()["missing_fields"] == [
        "screen_recording",
        "feature_flags",
    ]
    complete = client.post(
        url,
        json={
            "answers": [
                {"field": "screen_recording", "answer": "https://example.test/rec"},
                {"field": "feature_flags", "answer": "DASHBOARD_NATIVE_FILTERS=true"},
            ],
            "override_rationale": "reporter sent the details by email",
        },
        headers=_cmd("rr-2", _version(client, issue["id"]), **OPERATOR),
    )
    assert complete.status_code == 200, complete.text
    assert complete.json()["to_state"] == "reproducing"
    after = _detail(client, issue["id"]).json()
    assert after["workspace_live"] is True and after["missing_fields"] == []
    bad = client.post(
        url,
        json={"answers": [{"field": "nope", "answer": "x"}]},
        headers=_cmd("rr-3", _version(client, issue["id"]), **ORIGINAL_REPORTER),
    )
    assert bad.status_code == 422
    unknown_question = client.post(
        url,
        json={"question_id": str(uuid.UUID(int=1)), "response": {"kind": "answer", "value": "x"}},
        headers=_cmd("rr-4", _version(client, issue["id"]), **ORIGINAL_REPORTER),
    )
    assert unknown_question.status_code == 404
    stale = client.post(
        url,
        json={"issue_revision": 99, "answers": [{"field": "feature_flags", "answer": "x"}]},
        headers=_cmd("rr-5", _version(client, issue["id"]), **ORIGINAL_REPORTER),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_result"


def test_owner_reclassification_uses_pr8_vocabulary(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43231").json()["items"][0]
    resp = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json={"kind": "reclassify", "reclassify_as": "support", "rationale": "Config issue."},
        headers=_cmd("rc-1", issue["version"], **OWNER),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["to_state"] == "unsupported"


def test_owner_approval_is_authorized_against_routing(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-42991").json()["items"][0]
    pr = _detail(client, issue["id"]).json()["pull_requests"][0]
    body = {"kind": "approve_pr", "pr_number": pr["number"], "head_sha": pr["head_sha"]}
    unrouted = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json=body,
        headers=_cmd("ap-0", issue["version"], **UNROUTED_OWNER),
    )
    assert unrouted.status_code == 403
    assert unrouted.json()["error"]["code"] == "unauthorized_actor"
    approve = client.post(
        f"{API}/issues/{issue['id']}/decisions",
        json=body,
        headers=_cmd("ap-1", issue["version"], **OWNER),
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["to_state"] == "awaiting_owner"
    pr = _detail(client, issue["id"]).json()["pull_requests"][0]
    assert pr["review"] == "approved" and pr["human_approver"] == "export-owner"
    assert pr["approved_head_sha"] == pr["head_sha"]


def test_retry_requires_recoverable_state(client: TestClient) -> None:
    issue = _issue_by_key(client, "SUP-43207").json()["items"][0]
    resp = client.post(
        f"{API}/issues/{issue['id']}/actions/retry",
        json={},
        headers=_cmd("rt-1", issue["version"], **OPERATOR),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "illegal_transition"


def test_session_messages_and_cancel_bind_if_match_to_session_version(
    client: TestClient,
) -> None:
    running = _get(client, f"{API}/sessions", params={"status": "running"}).json()["items"][0]
    issue_version = _version(client, running["issue_id"])
    session_version = int(str(running["version"]))
    url = f"{API}/sessions/{running['id']}/messages"
    body = {"body": "Please attach the fixture manifest."}
    missing = client.post(url, json=body, headers=_cmd("m-0", **OPERATOR))
    assert missing.status_code == 422 and missing.json()["error"]["details"]["header"] == "If-Match"
    wrong = client.post(url, json=body, headers=_cmd("m-0b", session_version + 7, **OPERATOR))
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "version_conflict"
    details = wrong.json()["error"]["details"]
    assert details["resource"] == "session" and details["actual"] == session_version
    for i, headers in enumerate((REPORTER, OWNER, AGENT)):
        denied = client.post(url, json=body, headers=_cmd(f"m-0c-{i}", session_version, **headers))
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "unauthorized_actor"
    msg = client.post(url, json=body, headers=_cmd("m-1", session_version, **OPERATOR))
    assert msg.status_code == 200 and msg.json()["status"] == "noop", msg.text
    assert msg.json()["resource_id"] == running["id"]
    assert msg.json()["resource_version"] == session_version + 1
    detail = _get(client, f"{API}/sessions/{running['id']}").json()
    assert detail["version"] == session_version + 1
    assert detail["conversation"][-1]["author"]["kind"] == "operator"
    assert detail["conversation"][-1]["author"]["login"] == "ops-1"
    assert _version(client, running["issue_id"]) == issue_version
    stale = client.post(
        f"{API}/sessions/{running['id']}/actions/cancel",
        json={"reason": "budget"},
        headers=_cmd("c-0", session_version, **OPERATOR),
    )
    assert stale.status_code == 409
    for i, headers in enumerate((REPORTER, OWNER, AGENT)):
        denied = client.post(
            f"{API}/sessions/{running['id']}/actions/cancel",
            json={"reason": "budget"},
            headers=_cmd(f"c-0b-{i}", session_version + 1, **headers),
        )
        assert denied.status_code == 403, denied.text
    cancel = client.post(
        f"{API}/sessions/{running['id']}/actions/cancel",
        json={"reason": "budget"},
        headers=_cmd("c-1", session_version + 1, **OPERATOR),
    )
    assert cancel.status_code == 200, cancel.text
    assert cancel.json()["resource_id"] == running["id"]
    assert cancel.json()["resource_version"] == session_version + 2
    after = _get(client, f"{API}/sessions/{running['id']}").json()
    assert after["cancel_requested"] is True and after["version"] == session_version + 2
    finished = _get(client, f"{API}/sessions", params={"status": "completed"}).json()["items"][0]
    late = client.post(
        f"{API}/sessions/{finished['id']}/messages",
        json={"body": "too late"},
        headers=_cmd("m-2", finished["version"], **OPERATOR),
    )
    assert late.status_code == 409
    missing_session = client.post(
        f"{API}/sessions/{uuid.UUID(int=0)}/messages",
        json={"body": "x"},
        headers=_cmd("m-3", 0, **OPERATOR),
    )
    assert missing_session.status_code == 404


def test_validation_errors_use_error_envelope(client: TestClient) -> None:
    resp = client.post(f"{API}/dry-runs", json={"nope": 1}, headers=_cmd("ve-1", **OPERATOR))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_input"
    assert resp.json()["error"]["details"]["errors"]
