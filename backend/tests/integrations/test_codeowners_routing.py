"""CODEOWNERS parsing and deterministic reviewer routing."""

from __future__ import annotations

import pytest

from app.integrations import (
    ContractValidationError,
    OwnerKind,
    ReviewerRouter,
    ReviewerRoutingPolicy,
    RoutingStatus,
    parse_codeowners,
    routing_summary,
)

CODEOWNERS = """
# Superset ownership
*                       @exloong/core-maintainers
/superset/charts/       @alice @exloong/viz-team
/superset/db_engine_specs/  @bob
/docs/                  docs@example.com
"""


def test_parses_users_teams_and_email_owners() -> None:
    codeowners = parse_codeowners(CODEOWNERS)

    kinds = {owner.kind for rule in codeowners.rules for owner in rule.owners}
    assert kinds == {OwnerKind.USER, OwnerKind.TEAM, OwnerKind.EMAIL}


def test_comments_and_incomplete_lines_are_ignored() -> None:
    codeowners = parse_codeowners("# only a comment\n/orphan-pattern\n\n")

    assert codeowners.rules == ()


def test_last_matching_rule_wins() -> None:
    codeowners = parse_codeowners(CODEOWNERS)

    owners = codeowners.owners_for_path("superset/charts/api.py")

    assert {owner.handle for owner in owners} == {
        "alice",
        "exloong/viz-team",
    }


def test_resolved_routing_reports_candidates_and_rationale() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners(CODEOWNERS))

    decision = router.route(["superset/charts/api.py", "superset/charts/data.py"])

    assert decision.status is RoutingStatus.RESOLVED
    assert decision.requested_reviewers == ("alice",)
    assert decision.requested_team_reviewers == ("viz-team",)
    assert all(".github/CODEOWNERS:" in line for line in decision.rationale)
    assert "superset/charts/api.py" in decision.rationale[0]


def test_routing_is_deterministic_for_identical_inputs() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners(CODEOWNERS))
    paths = ["superset/charts/data.py", "superset/charts/api.py"]

    assert router.route(paths) == router.route(list(reversed(paths)))


def test_too_many_owner_groups_is_reported_as_ambiguous() -> None:
    router = ReviewerRouter(
        codeowners=parse_codeowners(CODEOWNERS),
        policy=ReviewerRoutingPolicy(max_owner_groups=1),
    )

    decision = router.route(
        ["superset/charts/api.py", "superset/db_engine_specs/mysql.py"]
    )

    assert decision.status is RoutingStatus.AMBIGUOUS
    assert decision.requested_reviewers == ()
    assert "owner must decide" in decision.reason
    assert len(decision.owner_groups) == 2


def test_email_only_ownership_yields_no_eligible_reviewer() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners("/docs/ docs@example.com"))

    decision = router.route(["docs/intro.mdx"])

    assert decision.status is RoutingStatus.NO_OWNER
    assert decision.unowned_paths == ("docs/intro.mdx",)
    assert decision.requested_reviewers == ()


def test_unowned_paths_are_reported_rather_than_guessed() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners("/superset/charts/ @alice"))

    decision = router.route(["superset/charts/api.py", "scripts/build.sh"])

    assert decision.status is RoutingStatus.AMBIGUOUS
    assert decision.unowned_paths == ("scripts/build.sh",)
    assert decision.requested_reviewers == ()


def test_excluded_logins_are_not_selected() -> None:
    router = ReviewerRouter(
        codeowners=parse_codeowners("* @alice @bob"),
        policy=ReviewerRoutingPolicy(excluded_logins=frozenset({"alice"})),
    )

    decision = router.route(["superset/app.py"])

    assert decision.requested_reviewers == ("bob",)


def test_reviewer_count_is_capped_by_policy() -> None:
    router = ReviewerRouter(
        codeowners=parse_codeowners("* @alice @bob @carol"),
        policy=ReviewerRoutingPolicy(max_reviewers=2),
    )

    decision = router.route(["superset/app.py"])

    assert len(decision.candidates) == 2


def test_routing_requires_at_least_one_changed_path() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners(CODEOWNERS))

    with pytest.raises(ContractValidationError):
        router.route([])


def test_routing_summary_is_serializable_for_the_dashboard() -> None:
    router = ReviewerRouter(codeowners=parse_codeowners(CODEOWNERS))

    summary = routing_summary(router.route(["superset/charts/api.py"]))

    candidates = summary["candidates"]
    assert isinstance(candidates, list)
    assert summary["status"] == "resolved"
    assert candidates[0]["rule"].startswith(".github/CODEOWNERS:")
