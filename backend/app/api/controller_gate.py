"""Deterministic intake gate for GitHub ``issues`` webhooks.

The gate decides whether a verified webhook may enter the lifecycle. It never
calls providers: repository, sender, issue state, and label are compared to a
policy loaded from the environment. Rejections are no-ops that are logged with
structured fields so an operator can see why an event was ignored.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.states import TARGET_REPOSITORY

LOGGER = logging.getLogger("relay.controller_gate")
ACCEPTED_ACTIONS = frozenset({"opened", "labeled"})


@dataclass(frozen=True)
class GatePolicy:
    repository: str = TARGET_REPOSITORY
    trusted_label: str | None = None
    trusted_actors: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> GatePolicy:
        label = os.environ.get("RELAY_TRUSTED_LABEL", "").strip()
        actors = {
            actor.strip().lower()
            for actor in os.environ.get("RELAY_TRUSTED_ACTORS", "").split(",")
            if actor.strip()
        }
        return cls(trusted_label=label or None, trusted_actors=frozenset(actors))


@dataclass(frozen=True)
class GateDecision:
    accepted: bool
    reason: str
    details: dict[str, object]


def evaluate_issue_event(
    policy: GatePolicy,
    *,
    repository: str,
    action: str,
    issue_state: str,
    labels: Sequence[str],
    label_added: str | None,
    sender: str,
) -> GateDecision:
    details: dict[str, object] = {
        "repository": repository,
        "action": action,
        "issue_state": issue_state,
        "sender": sender,
    }
    if repository.lower() != policy.repository.lower():
        return GateDecision(False, "repository_not_allowed", details)
    if action not in ACCEPTED_ACTIONS:
        return GateDecision(False, "action_not_gated", details)
    if issue_state != "open":
        return GateDecision(False, "issue_not_open", details)
    if policy.trusted_actors and sender.lower() not in policy.trusted_actors:
        return GateDecision(False, "untrusted_actor", details)
    if policy.trusted_label is None:
        if action == "labeled":
            return GateDecision(False, "no_trusted_label_configured", details)
        return GateDecision(True, "opened_without_label_policy", details)
    wanted = policy.trusted_label.lower()
    if action == "labeled":
        if (label_added or "").lower() != wanted:
            return GateDecision(False, "label_not_trusted", {**details, "label": label_added})
        return GateDecision(True, "trusted_label_added", details)
    if wanted not in {label.lower() for label in labels}:
        return GateDecision(False, "trusted_label_missing", {**details, "labels": list(labels)})
    return GateDecision(True, "opened_with_trusted_label", details)


def log_decision(decision: GateDecision, delivery_id: str) -> None:
    LOGGER.info(
        "controller gate %s: %s",
        "accepted" if decision.accepted else "rejected",
        decision.reason,
        extra={"delivery_id": delivery_id, "gate": decision.reason, **decision.details},
    )
