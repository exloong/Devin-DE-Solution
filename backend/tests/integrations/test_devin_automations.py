from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from app.integrations import FakeDevinSessionAdapter, LiveDevinSessionClient
from app.integrations.devin_automations import (
    DEVIN_COMMENT_MARKER_PREFIX,
    FIX_OPENED_MARKER,
    NEEDS_INFORMATION_MARKER,
    NOT_A_BUG_MARKER,
    REPRODUCED_MARKER,
    AutomationHandle,
    AutomationPatch,
    AutomationSpec,
    FakeDevinAutomationClient,
    LiveDevinAutomationClient,
    automation_metadata,
    automation_output_schema,
    automation_prompt,
    automation_triggers,
    fix_trigger_conditions,
    follow_up_trigger_conditions,
    native_issue_number,
    parse_native_fix_output,
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

# Fields Devin's live event schemas expose for github:issues / github:issue_comment.
SUPPORTED_FIELDS = {
    "action",
    "issue.title",
    "issue.body",
    "issue.user.login",
    "label.name",
    "comment.body",
    "comment.user.login",
    "comment.path",
    "repository.full_name",
}


def client(transport: RecordedTransport) -> LiveDevinAutomationClient:
    sessions = LiveDevinSessionClient(transport=transport, token_provider=TOKEN, org_id=ORG_ID)
    return LiveDevinAutomationClient(
        transport=transport, token_provider=TOKEN, org_id=ORG_ID, sessions=sessions
    )


def inbox_doc(*, automation_id: str = "auto-1", metadata: TaskKind | None = None) -> dict:
    """An operator-created (or legacy Relay) automation on a webhook inbox."""
    return {
        "automation_id": automation_id,
        "name": "Relay",
        "enabled": True,
        "metadata": dict(automation_metadata(metadata)) if metadata else {},
        "triggers": [
            {
                "trigger_id": "t1",
                "event_type": "webhook:incoming",
                "webhook": {"url": INBOX, "secret": "inbox-secret"},
            }
        ],
    }


def native_doc(kind: TaskKind, *, automation_id: str = "auto-1", prompt: str | None = None) -> dict:
    return {
        "automation_id": automation_id,
        "name": "Relay",
        "enabled": True,
        "metadata": dict(automation_metadata(kind)),
        "triggers": [
            {"trigger_id": f"t{i}", **trigger}
            for i, trigger in enumerate(automation_triggers(kind), start=1)
        ],
        "actions": [
            {"type": "start_session", "prompt": prompt or automation_prompt(kind)},
        ],
    }


def page(items: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {"items": list(items), "has_next_page": False, "end_cursor": None}


def _fields(conditions: dict) -> set[str]:
    return {cond["field"] for group in conditions["any"] for cond in group["all"]}


def test_prompts_make_devin_own_every_public_update() -> None:
    repro = automation_prompt(TaskKind.REPRODUCTION)
    assert "classify" in repro.lower()
    assert "context_completeness >= 80" in repro
    assert "never open a pull request" in repro
    assert NOT_A_BUG_MARKER in repro
    assert NEEDS_INFORMATION_MARKER in repro
    assert REPRODUCED_MARKER in repro
    assert "issue_comment" in repro
    assert "Never write a Devin session link" in repro
    assert "task_id" not in repro and "Relay posts" not in repro

    fix = automation_prompt(TaskKind.FIX)
    assert "@exloong/superset" in fix
    assert REPRODUCED_MARKER in fix
    assert FIX_OPENED_MARKER in fix
    assert "Fixes #<issue.number>" in fix
    assert "Never merge" in fix
    assert "Never write a Devin session link" in fix
    assert "owner" not in fix.lower() and "task_id" not in fix
    with pytest.raises(ContractValidationError):
        automation_prompt(TaskKind.CLASSIFICATION)


def test_prompt_schemas_are_embedded_and_name_the_issue() -> None:
    for kind in (TaskKind.REPRODUCTION, TaskKind.FIX):
        schema = automation_output_schema(kind)
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["issue_number", "repository", "phase"]
        assert "task_id" not in schema["properties"]  # type: ignore[operator]
        assert json.dumps(schema, sort_keys=True) in automation_prompt(kind)


def test_triggers_chain_natively_inside_devin_using_supported_fields() -> None:
    assert reproduction_trigger_conditions() == {
        "any": [
            {
                "all": [
                    {
                        "field": "repository.full_name",
                        "operator": "eq",
                        "value": "exloong/superset",
                    },
                    {"field": "action", "operator": "eq", "value": "opened"},
                ]
            }
        ]
    }
    repro = automation_triggers(TaskKind.REPRODUCTION)
    assert [t["event_type"] for t in repro] == ["github:issues", "github:issue_comment"]
    follow_up = repro[1]["conditions"]
    assert {
        "field": "comment.body",
        "operator": "not_contains",
        "value": DEVIN_COMMENT_MARKER_PREFIX,
    } in follow_up["any"][0]["all"]
    assert REPRODUCED_MARKER.startswith(DEVIN_COMMENT_MARKER_PREFIX)

    fix = automation_triggers(TaskKind.FIX)
    assert [t["event_type"] for t in fix] == ["github:issue_comment"]
    assert fix[0]["conditions"] == fix_trigger_conditions()
    assert {"field": "comment.body", "operator": "contains", "value": REPRODUCED_MARKER} in fix[0][
        "conditions"
    ]["any"][0]["all"]

    for conditions in (
        reproduction_trigger_conditions(),
        follow_up_trigger_conditions(),
        fix_trigger_conditions(),
    ):
        assert _fields(conditions) <= SUPPORTED_FIELDS
        assert "webhook" not in json.dumps(conditions)


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


def test_native_fix_output_requires_a_superset_pull_request() -> None:
    done = parse_native_fix_output(
        {
            "issue_number": 9,
            "repository": "exloong/superset",
            "phase": "done",
            "summary": "root cause: off by one",
            "pull_request": {
                "number": 42,
                "url": "https://github.com/exloong/superset/pull/42",
                "head_branch": "devin/9-fix",
            },
        }
    )
    assert done.pull_request is not None and done.pull_request.number == 42
    assert done.summary == "root cause: off by one"

    blocked = parse_native_fix_output(
        {
            "issue_number": 9,
            "repository": "exloong/superset",
            "phase": "done",
            "blocked_reason": "x",
        }
    )
    assert blocked.pull_request is None and blocked.blocked_reason == "x"

    with pytest.raises(ContractValidationError):
        parse_native_fix_output(None)
    with pytest.raises(ContractValidationError):
        parse_native_fix_output({"phase": "done"})
    with pytest.raises(ContractValidationError):
        parse_native_fix_output(
            {
                "issue_number": 9,
                "pull_request": {
                    "number": 1,
                    "url": "https://github.com/other/repo/pull/1",
                    "head_branch": "b",
                },
            }
        )


@pytest.mark.parametrize("kind", [TaskKind.REPRODUCTION, TaskKind.FIX])
def test_ensure_creates_native_automation_without_inbox(kind: TaskKind) -> None:
    transport = RecordedTransport(
        [HttpResponse(200, page([])), HttpResponse(201, native_doc(kind))]
    )
    automations = client(transport)

    handle = automations.ensure_automation(kind, now=NOW)

    assert handle == AutomationHandle("auto-1", kind)
    assert handle.native is True
    listing, create = transport.requests
    assert listing.method == "GET"
    assert listing.query["metadata.relay_kind"] == kind.value
    assert create.method == "POST"
    body = create.json_body
    assert body is not None
    assert body["run_as"] == {"type": "organization"}
    assert body["triggers"] == automation_triggers(kind)
    assert body["metadata"] == {"relay_kind": kind.value, "relay_repo": "exloong/superset"}
    assert "webhook" not in json.dumps(body)


def test_ensure_migrates_legacy_inbox_fix_automation_in_place() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(200, page([inbox_doc(metadata=TaskKind.FIX)])),
            HttpResponse(200, native_doc(TaskKind.FIX)),
        ]
    )
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.FIX, now=NOW)

    assert handle.automation_id == "auto-1"
    listing, patch = transport.requests
    assert patch.method == "PATCH" and patch.url == f"{API}/automations/auto-1"
    assert patch.json_body is not None
    assert patch.json_body["triggers"] == automation_triggers(TaskKind.FIX)
    assert patch.json_body["actions"][0]["prompt"] == automation_prompt(TaskKind.FIX)


def test_ensure_repairs_stale_prompt_but_never_recreates() -> None:
    stale = native_doc(TaskKind.REPRODUCTION, prompt="old prompt")
    stale["enabled"] = False
    transport = RecordedTransport(
        [HttpResponse(200, page([stale])), HttpResponse(200, native_doc(TaskKind.REPRODUCTION))]
    )
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.REPRODUCTION, now=NOW)

    assert handle.enabled is False
    assert [r.method for r in transport.requests] == ["GET", "PATCH"]


def test_ensure_reuses_current_automation_without_patching_it() -> None:
    transport = RecordedTransport([HttpResponse(200, page([native_doc(TaskKind.FIX)]))])
    automations = client(transport)

    handle = automations.ensure_automation(TaskKind.FIX, now=NOW)

    assert handle == AutomationHandle("auto-1", TaskKind.FIX, enabled=True)
    assert [r.method for r in transport.requests] == ["GET"]


def test_ensure_rejects_provider_that_drops_a_trigger() -> None:
    partial = native_doc(TaskKind.REPRODUCTION)
    partial["triggers"] = partial["triggers"][:1]
    transport = RecordedTransport([HttpResponse(200, page([])), HttpResponse(201, partial)])
    with pytest.raises(ContractValidationError):
        client(transport).ensure_automation(TaskKind.REPRODUCTION, now=NOW)


def test_management_list_get_create_update_delete_redact_secrets() -> None:
    created = inbox_doc(automation_id="new")
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
    transport = RecordedTransport(
        [
            HttpResponse(200, page([native_doc(TaskKind.REPRODUCTION), created])),
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
    assert listed[0].has_inbox is False
    assert [t.event_type for t in listed[0].triggers] == ["github:issues", "github:issue_comment"]
    assert listed[1].relay_kind is None
    assert listed[1].created_by == "Ops"
    assert listed[1].last_invocation_status == "succeeded"
    assert listed[1].has_inbox is True
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


def test_fake_client_only_lists_sessions_devin_started_itself() -> None:
    sessions = FakeDevinSessionAdapter()
    fake = FakeDevinAutomationClient(sessions)
    repro = fake.ensure_automation(TaskKind.REPRODUCTION, now=NOW)
    fix = fake.ensure_automation(TaskKind.FIX, now=NOW)
    assert not hasattr(fake, "dispatch")

    fake.simulate_native_session(
        make_task(TaskKind.REPRODUCTION),
        session_id="devin-r1",
        structured_output={"issue_number": 9, "repository": "exloong/superset", "phase": "triage"},
    )
    fake.simulate_native_session(
        make_task(TaskKind.FIX),
        session_id="devin-f1",
        kind=TaskKind.FIX,
        structured_output={"issue_number": 9, "repository": "exloong/superset", "phase": "fixing"},
    )

    assert [r.session_id for r in fake.list_automation_sessions(repro.automation_id)] == [
        "devin-r1"
    ]
    assert [r.session_id for r in fake.list_automation_sessions(fix.automation_id)] == ["devin-f1"]
    assert fake.get_automation(fix.automation_id).last_invocation_status == "succeeded"
