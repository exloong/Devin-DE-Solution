from __future__ import annotations

import pytest
from app.api.controller_gate import GatePolicy, evaluate_issue_event
from app.domain.states import TARGET_REPOSITORY


def _decide(
    policy: GatePolicy,
    *,
    repository: str = TARGET_REPOSITORY,
    action: str = "opened",
    issue_state: str = "open",
    labels: list[str] | None = None,
    label_added: str | None = None,
    sender: str = "reporter-1",
) -> tuple[bool, str]:
    decision = evaluate_issue_event(
        policy,
        repository=repository,
        action=action,
        issue_state=issue_state,
        labels=labels or [],
        label_added=label_added,
        sender=sender,
    )
    return decision.accepted, decision.reason


def test_default_policy_accepts_opened_issues_only() -> None:
    policy = GatePolicy()
    assert _decide(policy) == (True, "opened_without_label_policy")
    assert _decide(policy, action="labeled", label_added="bug") == (
        False,
        "no_trusted_label_configured",
    )
    assert _decide(policy, action="closed") == (False, "action_not_gated")
    assert _decide(policy, issue_state="closed") == (False, "issue_not_open")
    assert _decide(policy, repository="apache/superset") == (False, "repository_not_allowed")


def test_trusted_label_policy_gates_both_actions() -> None:
    policy = GatePolicy(trusted_label="relay:triage")
    assert _decide(policy) == (False, "trusted_label_missing")
    assert _decide(policy, labels=["Relay:Triage"]) == (True, "opened_with_trusted_label")
    assert _decide(policy, action="labeled", label_added="relay:triage") == (
        True,
        "trusted_label_added",
    )
    assert _decide(policy, action="labeled", label_added="bug") == (False, "label_not_trusted")


def test_trusted_actors_are_enforced_case_insensitively() -> None:
    policy = GatePolicy(trusted_actors=frozenset({"maintainer"}))
    assert _decide(policy, sender="Maintainer") == (True, "opened_without_label_policy")
    assert _decide(policy, sender="stranger") == (False, "untrusted_actor")


def test_policy_is_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELAY_TRUSTED_LABEL", " relay:triage ")
    monkeypatch.setenv("RELAY_TRUSTED_ACTORS", "Alice, bob ,")
    policy = GatePolicy.from_env()
    assert policy.trusted_label == "relay:triage"
    assert policy.trusted_actors == frozenset({"alice", "bob"})
    monkeypatch.delenv("RELAY_TRUSTED_LABEL")
    monkeypatch.delenv("RELAY_TRUSTED_ACTORS")
    assert GatePolicy.from_env() == GatePolicy()
