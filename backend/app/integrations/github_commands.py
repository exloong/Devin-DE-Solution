"""Typed GitHub side-effect commands restricted to ``exloong/superset``.

Every public write Relay may perform is one of the command dataclasses in this
module. Merging a pull request, closing an issue as fixed, publishing a
suspected security report, and executing reporter-provided content have no
command type, so no adapter can perform them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .errors import ContractValidationError, ValidationCode
from .repository import (
    RepositoryIdentity,
    TargetCommit,
    validate_branch_name,
    validate_superset_repository_field,
)

_LABEL_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,50}$")
_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_TEAM_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MAX_COMMENT_BYTES = 60_000
MAX_TITLE_LENGTH = 256
MAX_BODY_LENGTH = 60_000

PROHIBITED_COMMANDS: frozenset[str] = frozenset(
    {
        "merge_pull_request",
        "squash_merge_pull_request",
        "close_issue_as_fixed",
        "publish_security_report",
        "execute_reporter_script",
        "download_reporter_attachment",
    }
)


class GitHubCapability(str, Enum):
    """A separately authorized GitHub write capability."""

    COMMENT = "comment"
    LABEL = "label"
    BRANCH = "branch"
    PULL_REQUEST = "pull_request"
    PULL_REQUEST_LINKAGE = "pull_request_linkage"
    REVIEW_REQUEST = "review_request"


READ_ONLY_CAPABILITIES: frozenset[GitHubCapability] = frozenset()


def assert_command_supported(command_name: str) -> None:
    """Reject any attempt to invoke a capability Relay must never have."""
    if command_name in PROHIBITED_COMMANDS:
        raise ContractValidationError(
            ValidationCode.PROHIBITED_COMMAND,
            f"command {command_name} is prohibited by the Relay boundary",
        )


def safe_public_text(value: object, *, field_name: str, max_length: int) -> str:
    """Validate text destined for a public Superset write."""
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, f"{field_name} must be non-empty text"
        )
    if len(value) > max_length:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} exceeds {max_length} characters",
        )
    if _CONTROL_CHARACTERS.search(value):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} contains control characters",
        )
    return value


def quote_untrusted_text(value: str, *, max_length: int = 2_000) -> str:
    """Return reporter-provided text as inert quoted Markdown.

    The result is only ever used as comment data. Relay never passes reporter
    text to a shell, a prompt-level instruction, or a command argument.
    """
    if not isinstance(value, str):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, "untrusted text must be text"
        )
    collapsed = _CONTROL_CHARACTERS.sub(" ", value).replace("\r\n", "\n")
    truncated = collapsed[:max_length]
    lines = truncated.split("\n") or [""]
    return "\n".join(f"> {line}" for line in lines)


@dataclass(frozen=True)
class _SupersetCommand:
    repository: RepositoryIdentity

    def __post_init__(self) -> None:
        validate_superset_repository_field(self.repository)

    @property
    def capability(self) -> GitHubCapability:  # pragma: no cover - overridden
        raise NotImplementedError


def _require_issue_number(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} must be a positive integer",
        )
    return value


@dataclass(frozen=True)
class PostIssueComment(_SupersetCommand):
    """Publish one focused comment on a Superset issue."""

    issue_number: int
    body: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_issue_number(self.issue_number, "issue_number")
        safe_public_text(self.body, field_name="body", max_length=MAX_COMMENT_BYTES)

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.COMMENT


@dataclass(frozen=True)
class AddIssueLabels(_SupersetCommand):
    """Add lifecycle labels to a Superset issue."""

    issue_number: int
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_issue_number(self.issue_number, "issue_number")
        if not self.labels or len(self.labels) > 20:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "labels must contain 1 to 20 values"
            )
        if len(set(self.labels)) != len(self.labels):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "labels must be unique"
            )
        if any(not _LABEL_PATTERN.fullmatch(label) for label in self.labels):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "label is malformed"
            )

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.LABEL


@dataclass(frozen=True)
class CreateBranch(_SupersetCommand):
    """Create a fix branch from an immutable Superset commit."""

    branch_name: str
    base_commit: TargetCommit

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_branch_name(self.branch_name)
        if not isinstance(self.base_commit, TargetCommit):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "base_commit must be a TargetCommit",
            )

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.BRANCH


@dataclass(frozen=True)
class CreatePullRequest(_SupersetCommand):
    """Open a fix pull request in Superset. Draft by default; never merged."""

    head_branch: str
    base_branch: str
    title: str
    body: str
    source_issue_number: int
    draft: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        validate_branch_name(self.head_branch)
        validate_branch_name(self.base_branch)
        if self.head_branch == self.base_branch:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "head_branch and base_branch must differ",
            )
        safe_public_text(self.title, field_name="title", max_length=MAX_TITLE_LENGTH)
        safe_public_text(self.body, field_name="body", max_length=MAX_BODY_LENGTH)
        _require_issue_number(self.source_issue_number, "source_issue_number")

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.PULL_REQUEST


@dataclass(frozen=True)
class LinkPullRequestToIssue(_SupersetCommand):
    """Record the issue/pull-request relationship as a public comment."""

    issue_number: int
    pull_request_number: int

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_issue_number(self.issue_number, "issue_number")
        _require_issue_number(self.pull_request_number, "pull_request_number")

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.PULL_REQUEST_LINKAGE

    @property
    def comment_body(self) -> str:
        return (
            f"Relay opened #{self.pull_request_number} for issue "
            f"#{self.issue_number}. Human review and merge remain required."
        )


@dataclass(frozen=True)
class RequestReviewers(_SupersetCommand):
    """Request human review on a Superset pull request."""

    pull_request_number: int
    reviewers: tuple[str, ...] = ()
    team_reviewers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_issue_number(self.pull_request_number, "pull_request_number")
        if not self.reviewers and not self.team_reviewers:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "at least one reviewer or team reviewer is required",
            )
        if len(self.reviewers) + len(self.team_reviewers) > 15:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "too many review requests"
            )
        if len(set(self.reviewers)) != len(self.reviewers) or len(
            set(self.team_reviewers)
        ) != len(self.team_reviewers):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "review requests must be unique"
            )
        if any(not _LOGIN_PATTERN.fullmatch(login) for login in self.reviewers):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "reviewer login is malformed"
            )
        if any(not _TEAM_SLUG_PATTERN.fullmatch(slug) for slug in self.team_reviewers):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "reviewer team slug is malformed"
            )

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.REVIEW_REQUEST


GitHubCommand = (
    PostIssueComment
    | AddIssueLabels
    | CreateBranch
    | CreatePullRequest
    | LinkPullRequestToIssue
    | RequestReviewers
)

SUPPORTED_COMMAND_TYPES: tuple[type, ...] = (
    PostIssueComment,
    AddIssueLabels,
    CreateBranch,
    CreatePullRequest,
    LinkPullRequestToIssue,
    RequestReviewers,
)


@dataclass(frozen=True)
class CommentPosted:
    issue_number: int
    comment_id: int
    html_url: str


@dataclass(frozen=True)
class LabelsApplied:
    issue_number: int
    labels: tuple[str, ...]


@dataclass(frozen=True)
class BranchCreated:
    branch_name: str
    commit: TargetCommit


@dataclass(frozen=True)
class PullRequestOpened:
    pull_request_number: int
    html_url: str
    head_branch: str
    base_branch: str
    draft: bool
    source_issue_number: int


@dataclass(frozen=True)
class PullRequestLinked:
    issue_number: int
    pull_request_number: int
    comment_id: int


@dataclass(frozen=True)
class ReviewersRequested:
    pull_request_number: int
    accepted_reviewers: tuple[str, ...]
    accepted_team_reviewers: tuple[str, ...]
    rejected_reviewers: tuple[str, ...] = ()


CommandResult = (
    CommentPosted
    | LabelsApplied
    | BranchCreated
    | PullRequestOpened
    | PullRequestLinked
    | ReviewersRequested
)
