"""GitHub egress: allowed commands, prohibited capabilities, fake recording."""

from __future__ import annotations

import pytest
from conftest import OTHER_SHA, TARGET_SHA

from app.integrations import (
    PROHIBITED_COMMANDS,
    SUPERSET_REPOSITORY,
    AddIssueLabels,
    BranchCreated,
    CommentPosted,
    ContractValidationError,
    CreateBranch,
    CreatePullRequest,
    FakeGitHubAdapter,
    GitHubCapability,
    LinkPullRequestToIssue,
    PostIssueComment,
    PullRequestOpened,
    RequestReviewers,
    ReviewersRequested,
    TargetCommit,
    ValidationCode,
    assert_command_supported,
    github_commands,
    quote_untrusted_text,
    safe_public_text,
)


def test_only_the_six_authorized_command_types_exist() -> None:
    assert set(github_commands.SUPPORTED_COMMAND_TYPES) == {
        PostIssueComment,
        AddIssueLabels,
        CreateBranch,
        CreatePullRequest,
        LinkPullRequestToIssue,
        RequestReviewers,
    }
    assert {capability for capability in GitHubCapability} == {
        GitHubCapability.COMMENT,
        GitHubCapability.LABEL,
        GitHubCapability.BRANCH,
        GitHubCapability.PULL_REQUEST,
        GitHubCapability.PULL_REQUEST_LINKAGE,
        GitHubCapability.REVIEW_REQUEST,
    }


def test_no_module_attribute_implements_a_forbidden_capability() -> None:
    exported = {name.lower() for name in dir(github_commands)}
    for forbidden in ("merge", "closeissue", "publishsecurity", "executereporter"):
        assert not any(forbidden in name for name in exported)


@pytest.mark.parametrize("command_name", sorted(PROHIBITED_COMMANDS))
def test_prohibited_commands_are_rejected_by_name(command_name: str) -> None:
    with pytest.raises(ContractValidationError) as error:
        assert_command_supported(command_name)
    assert error.value.code is ValidationCode.PROHIBITED_COMMAND


def test_every_command_rejects_a_repository_other_than_superset() -> None:
    with pytest.raises(ContractValidationError) as error:
        PostIssueComment(
            repository="apache/superset",  # type: ignore[arg-type]
            issue_number=1,
            body="hello",
        )
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_comment_body_must_be_control_character_free_text() -> None:
    with pytest.raises(ContractValidationError):
        PostIssueComment(
            repository=SUPERSET_REPOSITORY, issue_number=1, body="bad\x00body"
        )
    with pytest.raises(ContractValidationError):
        PostIssueComment(repository=SUPERSET_REPOSITORY, issue_number=1, body="  ")


def test_issue_number_must_be_a_positive_non_boolean_integer() -> None:
    with pytest.raises(ContractValidationError):
        PostIssueComment(
            repository=SUPERSET_REPOSITORY,
            issue_number=True,
            body="hello",
        )
    with pytest.raises(ContractValidationError):
        PostIssueComment(repository=SUPERSET_REPOSITORY, issue_number=0, body="hi")


def test_labels_must_be_unique_and_bounded() -> None:
    with pytest.raises(ContractValidationError):
        AddIssueLabels(
            repository=SUPERSET_REPOSITORY, issue_number=1, labels=("a", "a")
        )
    with pytest.raises(ContractValidationError):
        AddIssueLabels(repository=SUPERSET_REPOSITORY, issue_number=1, labels=())


@pytest.mark.parametrize(
    "branch_name",
    ["../escape", "relay//fix", "relay/fix.lock", ".hidden", "relay/fix@{1}"],
)
def test_branch_names_reject_traversal_and_invalid_refs(branch_name: str) -> None:
    with pytest.raises(ContractValidationError):
        CreateBranch(
            repository=SUPERSET_REPOSITORY,
            branch_name=branch_name,
            base_commit=TargetCommit(sha=TARGET_SHA),
        )


def test_branch_requires_a_full_immutable_commit() -> None:
    with pytest.raises(ContractValidationError):
        TargetCommit(sha="abc1234")


def test_pull_requests_are_draft_by_default_and_need_distinct_branches() -> None:
    command = CreatePullRequest(
        repository=SUPERSET_REPOSITORY,
        head_branch="relay/fix-1",
        base_branch="master",
        title="Fix chart export",
        body="Relay prepared a scoped fix.",
        source_issue_number=40,
    )
    assert command.draft is True

    with pytest.raises(ContractValidationError):
        CreatePullRequest(
            repository=SUPERSET_REPOSITORY,
            head_branch="master",
            base_branch="master",
            title="t",
            body="b",
            source_issue_number=40,
        )


def test_linkage_comment_states_that_human_merge_is_required() -> None:
    command = LinkPullRequestToIssue(
        repository=SUPERSET_REPOSITORY, issue_number=40, pull_request_number=101
    )
    assert "merge remain required" in command.comment_body


def test_review_requests_require_at_least_one_reviewer() -> None:
    with pytest.raises(ContractValidationError):
        RequestReviewers(repository=SUPERSET_REPOSITORY, pull_request_number=101)


def test_reviewer_logins_and_team_slugs_are_validated() -> None:
    with pytest.raises(ContractValidationError):
        RequestReviewers(
            repository=SUPERSET_REPOSITORY,
            pull_request_number=101,
            reviewers=("not a login",),
        )


def test_untrusted_reporter_text_is_quoted_as_inert_data() -> None:
    quoted = quote_untrusted_text("rm -rf /\nsecond line")

    assert quoted.splitlines() == ["> rm -rf /", "> second line"]


def test_untrusted_text_is_truncated_and_stripped_of_control_characters() -> None:
    quoted = quote_untrusted_text("a\x00b" + "c" * 5_000, max_length=10)

    assert "\x00" not in quoted
    assert len(quoted) <= 12


def test_safe_public_text_enforces_a_length_ceiling() -> None:
    with pytest.raises(ContractValidationError):
        safe_public_text("x" * 11, field_name="body", max_length=10)


def test_fake_adapter_records_commands_without_network_writes() -> None:
    adapter = FakeGitHubAdapter()

    comment = adapter.execute(
        PostIssueComment(
            repository=SUPERSET_REPOSITORY, issue_number=40, body="Relay triage"
        )
    )
    branch = adapter.execute(
        CreateBranch(
            repository=SUPERSET_REPOSITORY,
            branch_name="relay/fix-40",
            base_commit=TargetCommit(sha=TARGET_SHA),
        )
    )
    opened = adapter.execute(
        CreatePullRequest(
            repository=SUPERSET_REPOSITORY,
            head_branch="relay/fix-40",
            base_branch="master",
            title="Fix chart export",
            body="Scoped fix plus regression test.",
            source_issue_number=40,
        )
    )

    assert isinstance(comment, CommentPosted)
    assert isinstance(branch, BranchCreated)
    assert isinstance(opened, PullRequestOpened)
    assert opened.draft is True
    assert opened.html_url.startswith("https://github.com/exloong/superset/pull/")
    assert len(adapter.recorded) == 3
    assert adapter.commands_for(GitHubCapability.COMMENT) == (
        PostIssueComment(
            repository=SUPERSET_REPOSITORY, issue_number=40, body="Relay triage"
        ),
    )


def test_fake_adapter_is_deterministic_across_instances() -> None:
    def run() -> int:
        adapter = FakeGitHubAdapter()
        result = adapter.execute(
            PostIssueComment(
                repository=SUPERSET_REPOSITORY, issue_number=40, body="Relay triage"
            )
        )
        assert isinstance(result, CommentPosted)
        return result.comment_id

    assert run() == run()


def test_fake_adapter_rejects_a_pull_request_without_its_branch() -> None:
    adapter = FakeGitHubAdapter()

    with pytest.raises(ContractValidationError):
        adapter.execute(
            CreatePullRequest(
                repository=SUPERSET_REPOSITORY,
                head_branch="relay/missing",
                base_branch="master",
                title="t",
                body="b",
                source_issue_number=40,
            )
        )


def test_fake_adapter_enforces_its_capability_allowlist() -> None:
    adapter = FakeGitHubAdapter(
        allowed_capabilities=frozenset({GitHubCapability.COMMENT})
    )

    with pytest.raises(ContractValidationError) as error:
        adapter.execute(
            CreateBranch(
                repository=SUPERSET_REPOSITORY,
                branch_name="relay/fix-40",
                base_commit=TargetCommit(sha=OTHER_SHA),
            )
        )
    assert error.value.code is ValidationCode.PROHIBITED_CAPABILITY
    assert adapter.recorded == ()


def test_fake_adapter_rejects_an_unsupported_command_object() -> None:
    adapter = FakeGitHubAdapter()

    with pytest.raises(ContractValidationError) as error:
        adapter.execute(object())  # type: ignore[arg-type]
    assert error.value.code is ValidationCode.PROHIBITED_COMMAND


def test_fake_adapter_reports_rejected_unknown_reviewers() -> None:
    adapter = FakeGitHubAdapter(known_reviewers=frozenset({"alice"}))

    result = adapter.execute(
        RequestReviewers(
            repository=SUPERSET_REPOSITORY,
            pull_request_number=101,
            reviewers=("alice", "ghost"),
        )
    )

    assert isinstance(result, ReviewersRequested)
    assert result.accepted_reviewers == ("alice",)
    assert result.rejected_reviewers == ("ghost",)
