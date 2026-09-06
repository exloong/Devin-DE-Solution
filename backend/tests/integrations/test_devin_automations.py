from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from app.integrations import FakeDevinSessionAdapter, LiveDevinSessionClient
from app.integrations.devin_automations import (
    WEBHOOK_SECRET_HEADER,
    AutomationHandle,
    AutomationPatch,
    AutomationSpec,
    FakeDevinAutomationClient,
    InMemoryAutomationSecretStore,
    LiveDevinAutomationClient,
    automation_metadata,
    automation_output_schema,
    automation_prompt,
    dispatch_payload,
    native_issue_number,
    parse_native_triage_output,
    reproduction_trigger_conditions,
)
from app.integrations.devin_sessions import canonical_session_url
from app.integrations.errors import ContractValidationError
from app.integrations.github_client import StaticTokenProvider
from app.integrations.json_values import JsonValue
from app.integrations.tasks import TaskKind
from app.integrations.transport import HttpResponse, RecordedTransport

from conftest import make_task

ORG_ID = "org-1edbfc26ef2d43d48516023aebe72dab"
API = f"https://api.devin.ai/v3/organizations/{ORG_ID}"
INBOX = "https://api.devin.ai/v3/webhooks/inbox/abc"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
TOKEN = StaticTokenProvider("devin-token")


def client(transport: RecordedTransport) -> LiveDevinAutomationClient:
    sessions = LiveDevinSessionClient(transport=transport, token_provider=TOKEN, org_id=ORG_ID)
    return LiveDevinAutomationClient(
        transport=transport,
        token_provider=TOKEN,
        org_id=ORG_ID,
        sessions=sessions,
        secret_store=InMemoryAutomationSecretStore(),
    )


def automation_doc(
    kind: TaskKind, *, secret: str | None = "inbox-secret", automation_id: str = "auto-1"
) -> dict[str, JsonValue]:
    webhook: dict[str, JsonValue] = {"url": INBOX}
    if secret is not None:
        webhook["secret"] = secret
    return {
        "automation_id": automation_id,
        "name": "Relay",
        "enabled": True,
        "metadata": dict(automation_metadata(kind)),
        "triggers": [{"trigger_id": "t1", "event_type": "webhook:incoming", "webhook": webhook}],
    }


def page(items: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {"items": list(items), "has_next_page": False, "end_cursor": None}


def test_prompts_separate_native_triage_from_owner_authorized_fix() -> None:
    fix = automation_prompt(TaskKind.FIX)
    assert "@exloong/superset" in fix
    assert "task_id" in fix
    assert "Never merge" in fix
    assert "human owner has confirmed" in fix
    repro = automation_prompt(TaskKind.REPRODUCTION)
    assert "GitHub `issues` event" in repro
    assert "context_completeness >= 80" in repro
    assert "never open a pull request" in repro
    assert "task_id" not in repro
    with pytest.raises(ContractValidationError):
        automation_prompt(TaskKind.CLASSIFICATION)


def test_prompt_schemas_match_each_kind_of_session() -> None:
    fix_schema = automation_output_schema(TaskKind.FIX)
    assert fix_schema["additionalProperties"] is False
    assert "task_id" in fix_schema["properties"]  # type: ignore[operator]
    assert "task_id" in fix_schema["required"]  # type: ignore[operator]
    assert json.dumps(fix_schema, sort_keys=True) in automation_prompt(TaskKind.FIX)

    native = automation_output_schema(TaskKind.REPRODUCTION)
    assert native["required"] == ["issue_number", "repository", "phase"]
    assert "task_id" not in native["properties"]  # type: ignore[operator]
    assert json.dumps(native, sort_keys=True) in automation_prompt(TaskKind.REPRODUCTION)


def test_native_trigger_conditions_encode_the_repository_and_label_gate() -> None:
    conditions = reproduction_trigger_conditions("bug")
    assert conditions == {
        "any": [
            {
                "all": [
                    {
                        "field": "repository.full_name",
                        "operator": "eq",
                        "value": "exloong/superset",
                    },
                    {"field": "action", "operator": "eq", "value": "labeled"},
                    {"field": "label.name", "operator": "eq", "value": "bug"},
                ]
            }
        ]
    }
    with pytest.raises(ContractValidationError):
        reproduction_trigger_conditions(" ")


def test_native_triage_output_applies_relay_context_gate() -> None:
    assert native_issue_number(None) is None
    assert native_issue_number({"issue_number": 0}) is None
    assert native_issue_number({"issue_number": True}) is None
    assert native_issue_number({"issue_number": 9, "repository": "other/repo"}) is None
    assert native_issue_number({"issue_number": 9}) == 9

    thin = parse_native_triage_output(
        {
            "issue_number": 9,
            "repository": "exloong/superset",
            "phase": "done",
            "classification": "bug",
            "context_completeness": 55,
            "missing_fields": [{"field": "version", "prompt": "Which version?"}],
        }
    )
    assert thin.classification == "bug"
    assert thin.effective_classification == "needs_information"
    assert thin.missing_fields[0].field == "version"
    assert thin.reproduction is None

    full = parse_native_triage_output(
        {
            "issue_number": 9,
            "repository": "exloong/superset",
            "phase": "done",
            "classification": "bug",
            "context_completeness": 90,
            "reproduction": {
                "reproduced": True,
                "attempts": 2,
                "observed_behavior": "raises",
                "target_behavior": "fails",
                "control_behavior": "passes",
            },
        }
    )
    assert full.effective_classification == "bug"
    assert full.reproduction is not None and full.reproduction.attempts == 2

    with pytest.raises(ContractValidationError):
        parse_native_triage_output({"issue_number": 9, "classification": "wontfix"})
    with pytest.raises(ContractValidationError):
        parse_native_triage_output({"issue_number": 9, "context_completeness": 120})
    with pytest.raises(ContractValidationError):
        parse_native_triage_output({"phase": "triage"})


def native_doc(*, automation_id: str = "auto-1", event_type: str = "github:issues") -> dict:
    return {
        "automation_id": automation_id,
        "name": "Relay",
        "enabled": True,
        "metadata": dict(automation_metadata(TaskKind.REPRODUCTION)),
        "triggers": [
            {
                "trigger_id": "t1",
                "event_type": event_type,
                "conditions": reproduction_trigger_conditions(),
            }
        ],
    }


def test_ensure_creates_native_reproduction_automation_without_inbox() -> None:
    transport = RecordedTransport([HttpResponse(200, page([])), HttpResponse(201, native_doc())])
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.REPRODUCTION, now=NOW)

    assert handle == AutomationHandle("auto-1", TaskKind.REPRODUCTION, "", None)
    assert handle.native is True and handle.can_dispatch is False
    listing, create = transport.requests
    assert listing.method == "GET"
    assert listing.query["metadata.relay_kind"] == "reproduction"
    assert create.method == "POST"
    body = create.json_body
    assert body is not None
    assert body["triggers"] == [
        {"event_type": "github:issues", "conditions": reproduction_trigger_conditions("bug")}
    ]
    assert body["metadata"] == {"relay_kind": "reproduction", "relay_repo": "exloong/superset"}
    assert automations.secret_store.load(TaskKind.REPRODUCTION) == handle


def test_ensure_migrates_legacy_inbox_reproduction_automation_in_place() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(200, page([automation_doc(TaskKind.REPRODUCTION, secret=None)])),
            HttpResponse(200, native_doc()),
        ]
    )
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.REPRODUCTION, now=NOW)

    assert handle.automation_id == "auto-1" and handle.inbox_secret is None
    listing, patch = transport.requests
    assert patch.method == "PATCH" and patch.url == f"{API}/automations/auto-1"
    assert patch.json_body is not None
    assert patch.json_body["triggers"] == [
        {"event_type": "github:issues", "conditions": reproduction_trigger_conditions("bug")}
    ]


def test_ensure_creates_fix_automation_when_none_exists_and_keeps_secret() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(200, page([])),
            HttpResponse(201, automation_doc(TaskKind.FIX)),
        ]
    )
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.FIX, now=NOW)

    assert handle == AutomationHandle("auto-1", TaskKind.FIX, INBOX, "inbox-secret")
    listing, create = transport.requests
    assert listing.query["metadata.relay_kind"] == "fix"
    body = create.json_body
    assert body is not None
    assert body["run_as"] == {"type": "organization"}
    assert body["triggers"] == [{"event_type": "webhook:incoming", "conditions": None}]
    actions = body["actions"]
    assert isinstance(actions, list) and len(actions) == 1
    action = actions[0]
    assert isinstance(action, dict)
    assert action["type"] == "start_session"
    assert body["metadata"] == {"relay_kind": "fix", "relay_repo": "exloong/superset"}
    assert automations.secret_store.load(TaskKind.FIX) == handle


def test_ensure_reuses_existing_automation_without_patching_it() -> None:
    doc = automation_doc(TaskKind.FIX, secret=None)
    doc["enabled"] = False
    transport = RecordedTransport([HttpResponse(200, page([doc]))])
    automations = client(transport)
    automations.secret_store.save(AutomationHandle("auto-1", TaskKind.FIX, INBOX, "kept"))

    handle = automations.ensure_automation(TaskKind.FIX, now=NOW)

    assert handle.inbox_secret == "kept"
    assert handle.enabled is False
    assert [r.method for r in transport.requests] == ["GET"]


def test_management_list_get_create_update_delete_redact_secrets() -> None:
    created = automation_doc(TaskKind.FIX, automation_id="new")
    created.update(
        {
            "name": "Nightly triage",
            "created_at": 1_767_268_800,
            "updated_at": 1_767_268_900,
            "created_by": {"id": "u1", "name": "Ops"},
            "last_invocation": {"status": "succeeded", "fired_at": 1_767_268_950},
            "actions": [{"type": "start_session", "prompt": "do the thing"}],
        }
    )
    del created["metadata"]
    transport = RecordedTransport(
        [
            HttpResponse(200, page([automation_doc(TaskKind.REPRODUCTION), created])),
            HttpResponse(200, created),
            HttpResponse(201, created),
            HttpResponse(200, created),
            HttpResponse(204, None),
        ]
    )
    automations = client(transport)

    listed = automations.list_automations()
    assert [a.automation_id for a in listed] == ["auto-1", "new"]
    assert listed[0].relay_kind is TaskKind.REPRODUCTION
    assert listed[1].relay_kind is None
    assert listed[1].created_by == "Ops"
    assert listed[1].last_invocation_status == "succeeded"
    assert listed[1].has_inbox is True
    assert listed[1].triggers[0].event_type == "webhook:incoming"
    assert "inbox-secret" not in repr(listed) and INBOX not in repr(listed)

    assert automations.get_automation("new").prompt == "do the thing"

    automations.create_automation(
        AutomationSpec(name="Nightly triage", prompt="do the thing", metadata={"team": "ops"})
    )
    body = transport.requests[2].json_body
    assert body is not None
    assert body["run_as"] == {"type": "organization"}
    assert body["triggers"] == [{"event_type": "webhook:incoming", "conditions": None}]
    assert body["metadata"] == {"team": "ops"}
    actions = body["actions"]
    assert isinstance(actions, list) and isinstance(actions[0], dict)
    assert actions[0]["session"] == {
        "bypass_approval": False,
        "tags": ["relay", "repo:exloong/superset", "launcher:automation"],
    }

    automations.update_automation("new", AutomationPatch(prompt="new prompt", enabled=False))
    patch = transport.requests[3]
    assert patch.method == "PATCH" and patch.url == f"{API}/automations/new"
    assert patch.json_body == {
        "enabled": False,
        "actions": [{"type": "start_session", "prompt": "new prompt"}],
    }

    automations.delete_automation("new")
    assert transport.requests[4].method == "DELETE"


def test_list_automation_sessions_is_lenient_about_untagged_sessions() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {
                    "items": [
                        {"session_id": "s-1", "created_at": 1_767_268_800, "status": "running"},
                        {
                            "session_id": "s-2",
                            "title": "Later",
                            "status": "finished",
                            "url": "https://app.devin.ai/sessions/s-2",
                            "created_at": 1_767_268_900,
                            "updated_at": 1_767_268_950,
                            "tags": ["relay", 7],
                        },
                    ],
                    "has_next_page": False,
                    "end_cursor": None,
                },
            )
        ]
    )
    rows = client(transport).list_automation_sessions("auto-1")
    assert transport.requests[0].query["automation_ids"] == "auto-1"
    assert [r.session_id for r in rows] == ["s-2", "s-1"]
    assert rows[1].title == "s-1" and rows[1].url == canonical_session_url("s-1")
    assert rows[0].tags == ("relay",)


def test_ensure_recreates_automation_whose_secret_was_never_seen() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200, page([automation_doc(TaskKind.FIX, secret=None, automation_id="old")])
            ),
            HttpResponse(204, None),
            HttpResponse(201, automation_doc(TaskKind.FIX, automation_id="new")),
        ]
    )
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.FIX, now=NOW)

    assert handle.automation_id == "new"
    assert handle.inbox_secret == "inbox-secret"
    assert [r.method for r in transport.requests] == ["GET", "DELETE", "POST"]
    assert transport.requests[1].url == f"{API}/automations/old"


def test_dispatch_posts_task_envelope_to_inbox_with_secret_header() -> None:
    transport = RecordedTransport([HttpResponse(202, {"accepted": True})])
    automations = client(transport)
    handle = AutomationHandle("auto-1", TaskKind.FIX, INBOX, "inbox-secret")
    task = make_task(TaskKind.FIX)

    receipt = automations.dispatch(handle, task, reporter_context="steps", now=NOW)

    assert receipt.automation_id == "auto-1"
    assert receipt.task_id == str(task.task_id)
    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url == INBOX
    assert request.headers[WEBHOOK_SECRET_HEADER] == "inbox-secret"
    assert "Authorization" not in request.headers
    assert request.json_body == dispatch_payload(task, reporter_context="steps")
    body = request.json_body["task"]
    assert isinstance(body, dict)
    assert body["task_id"] == str(task.task_id)
    assert body["target_commit"] == task.target_commit.sha


def test_dispatch_refuses_handle_without_secret() -> None:
    automations = client(RecordedTransport())
    handle = AutomationHandle("auto-1", TaskKind.FIX, INBOX, None)
    with pytest.raises(ContractValidationError):
        automations.dispatch(handle, make_task(TaskKind.FIX), now=NOW)


def test_dispatch_refuses_native_reproduction_automation() -> None:
    automations = client(RecordedTransport())
    handle = AutomationHandle("auto-1", TaskKind.REPRODUCTION, INBOX, "secret")
    with pytest.raises(ContractValidationError):
        automations.dispatch(handle, make_task(TaskKind.REPRODUCTION), now=NOW)
    assert automations.transport.requests == ()


def test_list_spawned_sessions_filters_by_automation_and_since() -> None:
    task = make_task(TaskKind.REPRODUCTION)

    def session(session_id: str, created: str) -> dict[str, JsonValue]:
        return {
            "session_id": session_id,
            "org_id": ORG_ID,
            "status": "running",
            "created_at": created,
            "updated_at": created,
            "tags": ["relay", "kind:reproduction", "repo:exloong/superset"],
            "url": canonical_session_url(session_id),
        }

    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                page(
                    [
                        session("devin-new", "2026-01-01T12:05:00Z"),
                        session("devin-old", "2025-12-31T00:00:00Z"),
                    ]
                ),
            )
        ]
    )
    automations = client(transport)

    spawned = automations.list_spawned_sessions("auto-1", since=NOW, task=task)

    assert [s.session_id for s in spawned] == ["devin-new"]
    assert spawned[0].kind is TaskKind.REPRODUCTION
    assert spawned[0].target_commit == task.target_commit
    request = transport.requests[0]
    assert request.url == f"{API}/sessions"
    assert request.query["automation_ids"] == "auto-1"


def test_fake_client_spawns_sessions_for_dispatches_in_order() -> None:
    sessions = FakeDevinSessionAdapter()
    fake = FakeDevinAutomationClient(sessions, auto_spawn=False)
    handle = fake.ensure_automation(TaskKind.FIX, now=NOW)
    first = make_task(TaskKind.FIX)
    second = make_task(TaskKind.FIX, task_id=uuid4())

    fake.dispatch(handle, first, now=NOW)
    fake.dispatch(handle, second, now=NOW)
    assert fake.list_spawned_sessions(handle.automation_id, since=NOW) == ()

    spawned = fake.spawn_pending(TaskKind.FIX)

    assert [s.task_id for s in spawned] == [str(first.task_id), str(second.task_id)]
    assert fake.dispatched(TaskKind.FIX) == (str(first.task_id), str(second.task_id))
