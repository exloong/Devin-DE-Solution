from __future__ import annotations

from collections.abc import Iterator

import pytest
from app.api.app import create_app, demo_automations, live_automations_from_env
from app.domain.states import ActorRole
from app.integrations import FakeDevinSessionAdapter
from app.integrations.devin_automations import (
    AutomationSpec,
    FakeDevinAutomationClient,
    LiveDevinAutomationClient,
)
from app.integrations.errors import ContractValidationError, ValidationCode
from app.integrations.tasks import TaskPolicy
from fastapi.testclient import TestClient

from tests.conftest import FakeClock, bearer, token_auth

API = "/api/v1/automations"
OPERATOR = bearer(ActorRole.OPERATOR)
AGENT = bearer(ActorRole.AGENT)
OWNER = bearer(ActorRole.OWNER)


@pytest.fixture
def automations(clock: FakeClock) -> FakeDevinAutomationClient:
    return demo_automations(clock)


@pytest.fixture
def api(clock: FakeClock, automations: FakeDevinAutomationClient) -> Iterator[TestClient]:
    app = create_app(
        database_url="sqlite:///:memory:",
        seed_scenarios=True,
        clock=clock,
        auth=token_auth(),
        automations=automations,
    )
    with TestClient(app) as c:
        yield c


def test_list_shows_relay_automations_without_inbox_details(api: TestClient) -> None:
    resp = api.get(API, headers=OPERATOR)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    kinds = {item["relay_kind"] for item in body["items"]}
    assert kinds == {"reproduction", "fix"}
    for item in body["items"]:
        assert item["managed_by_relay"] is True
        assert item["has_inbox"] is True
        assert item["event_types"] == ["webhook:incoming"]
        assert "task_id" in item["prompt"]
    text = resp.text
    assert "fake-secret" not in text and "webhooks/inbox" not in text


def test_reads_require_operator_class_and_writes_require_operator(api: TestClient) -> None:
    assert api.get(API, headers=OWNER).status_code == 403
    assert api.get(API, headers=AGENT).status_code == 403
    assert api.get(API).status_code == 401
    body = {"name": "x", "prompt": "y"}
    assert api.post(API, json=body, headers=OWNER).status_code == 403
    assert api.delete(f"{API}/auto-1", headers=AGENT).status_code == 403


def test_create_get_update_delete_round_trip(api: TestClient) -> None:
    created = api.post(
        API,
        json={
            "name": "Nightly flaky-test sweep",
            "prompt": "Find flaky tests in @exloong/superset and report them.",
            "description": "Operator-defined",
            "metadata": {"team": "qa"},
        },
        headers=OPERATOR,
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["managed_by_relay"] is False and item["relay_kind"] is None
    assert item["metadata"] == {"team": "qa"}
    automation_id = item["automation_id"]

    detail = api.get(f"{API}/{automation_id}", headers=OPERATOR)
    assert detail.status_code == 200
    assert detail.json()["automation"]["name"] == "Nightly flaky-test sweep"
    assert detail.json()["sessions"] == {
        "automation_id": automation_id,
        "relay_sessions": [],
        "provider_sessions": [],
        "provider_error": None,
        "generated_at": detail.json()["sessions"]["generated_at"],
    }

    updated = api.patch(
        f"{API}/{automation_id}",
        json={"enabled": False, "prompt": "Updated prompt"},
        headers=OPERATOR,
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["prompt"] == "Updated prompt"

    assert api.patch(f"{API}/{automation_id}", json={}, headers=OPERATOR).status_code == 422

    assert api.delete(f"{API}/{automation_id}", headers=OPERATOR).status_code == 204
    assert api.get(f"{API}/{automation_id}", headers=OPERATOR).status_code == 404
    assert api.get(API, headers=OPERATOR).json()["total"] == 2


def test_create_rejects_reserved_relay_metadata(api: TestClient) -> None:
    resp = api.post(
        API,
        json={"name": "x", "prompt": "y", "metadata": {"relay_kind": "fix"}},
        headers=OPERATOR,
    )
    assert resp.status_code == 422


def test_sessions_under_an_automation_include_provider_and_relay_rows(
    api: TestClient, automations: FakeDevinAutomationClient, clock: FakeClock
) -> None:
    listed = api.get(API, headers=OPERATOR).json()["items"]
    fix = next(a for a in listed if a["relay_kind"] == "fix")
    resp = api.get(f"{API}/{fix['automation_id']}/sessions", headers=OPERATOR)
    assert resp.status_code == 200
    assert resp.json()["provider_sessions"] == []
    assert resp.json()["relay_sessions"] == []


def test_provider_failure_is_reported_as_bad_gateway(clock: FakeClock) -> None:
    class Broken(FakeDevinAutomationClient):
        def list_automations(self) -> tuple[()]:
            raise ContractValidationError(
                ValidationCode.TRANSPORT_FAILURE,
                "list automations failed with status 403",
                upstream_status=403,
            )

    app = create_app(
        database_url="sqlite:///:memory:",
        clock=clock,
        auth=token_auth(),
        automations=Broken(FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400))),
    )
    with TestClient(app) as c:
        resp = c.get(API, headers=OPERATOR)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "transport_failure"
    assert resp.json()["error"]["upstream_status"] == 403


def test_provider_outage_is_reported_as_bad_gateway(clock: FakeClock) -> None:
    class Down(FakeDevinAutomationClient):
        def list_automations(self) -> tuple[()]:
            raise ContractValidationError(ValidationCode.TRANSPORT_FAILURE, "connection refused")

    app = create_app(
        database_url="sqlite:///:memory:",
        clock=clock,
        auth=token_auth(),
        automations=Down(FakeDevinSessionAdapter(policy=TaskPolicy(max_wall_seconds=5_400))),
    )
    with TestClient(app) as c:
        assert c.get(API, headers=OPERATOR).status_code == 502


def test_unconfigured_deployment_reports_missing_automations(client: TestClient) -> None:
    resp = client.get(API, headers=OPERATOR)
    assert resp.status_code == 403
    assert "not configured" in resp.json()["error"]["message"]


def test_live_client_only_when_credentials_present() -> None:
    assert live_automations_from_env({}) is None
    assert live_automations_from_env({"DEVIN_API_TOKEN": "t"}) is None
    live = live_automations_from_env(
        {"DEVIN_API_TOKEN": "t", "DEVIN_ORG_ID": "org-1edbfc26ef2d43d48516023aebe72dab"}
    )
    assert isinstance(live, LiveDevinAutomationClient)


def test_fake_create_spec_defaults(automations: FakeDevinAutomationClient) -> None:
    created = automations.create_automation(AutomationSpec(name="n", prompt="p"))
    assert created.enabled is True and created.has_inbox is True
