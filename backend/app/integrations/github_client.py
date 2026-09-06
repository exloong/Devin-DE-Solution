"""GitHub client boundary: a deterministic fake and a live REST client.

Both implementations accept only :mod:`github_commands` command objects, and
every command already validated that its target is ``exloong/superset``.
"""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as BinasciiError
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import count
from threading import Lock
from typing import Protocol

from .codeowners import CODEOWNERS_PATH, CodeownersFile
from .errors import ContractValidationError, ValidationCode
from .github_commands import (
    SUPPORTED_COMMAND_TYPES,
    AddIssueLabels,
    BranchCreated,
    CommandResult,
    CommentPosted,
    CreateBranch,
    CreatePullRequest,
    GitHubCapability,
    GitHubCommand,
    LabelsApplied,
    LinkPullRequestToIssue,
    PostIssueComment,
    PullRequestLinked,
    PullRequestOpened,
    RequestReviewers,
    ReviewersRequested,
)
from .json_values import JsonObject, JsonValue
from .repository import (
    SUPERSET_FULL_NAME,
    SUPERSET_REPOSITORY,
    RepositoryIdentity,
    TargetCommit,
    require_superset_repository,
    validate_issue_comment_url,
    validate_pull_request_url,
)
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TokenProvider,
    object_array,
    require_int,
    require_json_array,
    require_json_object,
    require_str,
    require_success,
)

GITHUB_API_ROOT = "https://api.github.com"
PULL_REQUEST_FILE_PAGE_SIZE = 100
MAX_PULL_REQUEST_FILE_PAGES = 30
MAX_PULL_REQUEST_FILES = PULL_REQUEST_FILE_PAGE_SIZE * MAX_PULL_REQUEST_FILE_PAGES


@dataclass(frozen=True)
class IssueSnapshot:
    """The parts of a Superset issue Relay needs to enroll it."""

    number: int
    title: str
    body: str
    state: str
    labels: tuple[str, ...]
    reporter_login: str


class GitHubClient(Protocol):
    """The only GitHub operations Relay is allowed to perform."""

    def execute(self, command: GitHubCommand) -> CommandResult:
        """Perform one authorized Superset write command."""

    def read_codeowners(self, ref: str) -> CodeownersFile:
        """Read and parse ``.github/CODEOWNERS`` at ``ref``."""

    def list_pull_request_files(self, pull_request_number: int) -> tuple[str, ...]:
        """List paths changed by a Superset pull request."""

    def get_branch_head(self, branch: str) -> TargetCommit:
        """Resolve a branch to an immutable commit."""

    def get_pull_request_head(self, pull_request_number: int) -> TargetCommit:
        """Resolve a pull request to its immutable current head."""

    def get_issue(self, issue_number: int) -> IssueSnapshot:
        """Read one Superset issue (title, body, state, labels, reporter)."""


def _authorize(
    command: GitHubCommand, allowed_capabilities: frozenset[GitHubCapability]
) -> GitHubCapability:
    if not isinstance(command, SUPPORTED_COMMAND_TYPES):
        raise ContractValidationError(
            ValidationCode.PROHIBITED_COMMAND,
            "unsupported GitHub command type",
        )
    require_superset_repository(command.repository)
    capability = command.capability
    if capability not in allowed_capabilities:
        raise ContractValidationError(
            ValidationCode.PROHIBITED_CAPABILITY,
            f"GitHub capability {capability.value} is not authorized",
        )
    return capability


@dataclass(frozen=True)
class RecordedCommand:
    """One command a fake adapter accepted instead of performing."""

    capability: GitHubCapability
    command: GitHubCommand
    result: CommandResult


class FakeGitHubAdapter:
    """Records commands deterministically without performing network writes."""

    def __init__(
        self,
        *,
        allowed_capabilities: frozenset[GitHubCapability] | None = None,
        codeowners_content: str = "",
        pull_request_files: Mapping[int, tuple[str, ...]] | None = None,
        pull_request_heads: Mapping[int, TargetCommit] | None = None,
        known_reviewers: frozenset[str] | None = None,
        branch_head: TargetCommit | None = None,
        issues: Mapping[int, IssueSnapshot] | None = None,
    ) -> None:
        if allowed_capabilities is not None and any(
            not isinstance(capability, GitHubCapability) for capability in allowed_capabilities
        ):
            raise ContractValidationError(
                ValidationCode.PROHIBITED_CAPABILITY,
                "GitHub capability allowlist contains an unsupported value",
            )
        self._allowed_capabilities = (
            frozenset(GitHubCapability)
            if allowed_capabilities is None
            else frozenset(allowed_capabilities)
        )
        self._codeowners_content = codeowners_content
        self._pull_request_files = dict(pull_request_files or {})
        self._pull_request_heads = dict(pull_request_heads or {})
        self._known_reviewers = known_reviewers
        self._branch_head = branch_head or TargetCommit(sha="0" * 40)
        self._issues = dict(issues or {})
        self._recorded: list[RecordedCommand] = []
        self._comment_ids = count(start=9_001)
        self._pull_request_numbers = count(start=101)
        self._branches: dict[str, TargetCommit] = {}
        self._lock = Lock()

    @property
    def repository(self) -> RepositoryIdentity:
        return require_superset_repository(SUPERSET_FULL_NAME)

    def execute(self, command: GitHubCommand) -> CommandResult:
        capability = _authorize(command, self._allowed_capabilities)
        with self._lock:
            result = self._perform(command)
            self._recorded.append(
                RecordedCommand(capability=capability, command=command, result=result)
            )
        return result

    def _perform(self, command: GitHubCommand) -> CommandResult:
        if isinstance(command, PostIssueComment):
            comment_id = next(self._comment_ids)
            return CommentPosted(
                issue_number=command.issue_number,
                comment_id=comment_id,
                html_url=(
                    f"https://github.com/{SUPERSET_FULL_NAME}/issues/"
                    f"{command.issue_number}#issuecomment-{comment_id}"
                ),
            )
        if isinstance(command, AddIssueLabels):
            return LabelsApplied(
                issue_number=command.issue_number,
                labels=tuple(sorted(command.labels)),
            )
        if isinstance(command, CreateBranch):
            self._branches[command.branch_name] = command.base_commit
            return BranchCreated(branch_name=command.branch_name, commit=command.base_commit)
        if isinstance(command, CreatePullRequest):
            if command.head_branch not in self._branches:
                raise ContractValidationError(
                    ValidationCode.MALFORMED_ENVELOPE,
                    f"head branch {command.head_branch} does not exist",
                )
            number = next(self._pull_request_numbers)
            return PullRequestOpened(
                pull_request_number=number,
                html_url=f"https://github.com/{SUPERSET_FULL_NAME}/pull/{number}",
                head_branch=command.head_branch,
                base_branch=command.base_branch,
                draft=command.draft,
                source_issue_number=command.source_issue_number,
            )
        if isinstance(command, LinkPullRequestToIssue):
            return PullRequestLinked(
                issue_number=command.issue_number,
                pull_request_number=command.pull_request_number,
                comment_id=next(self._comment_ids),
            )
        if isinstance(command, RequestReviewers):
            if self._known_reviewers is None:
                accepted = command.reviewers
                rejected: tuple[str, ...] = ()
            else:
                accepted = tuple(
                    login for login in command.reviewers if login in self._known_reviewers
                )
                rejected = tuple(
                    login for login in command.reviewers if login not in self._known_reviewers
                )
            return ReviewersRequested(
                pull_request_number=command.pull_request_number,
                accepted_reviewers=accepted,
                accepted_team_reviewers=command.team_reviewers,
                rejected_reviewers=rejected,
            )
        raise ContractValidationError(  # pragma: no cover - defensive
            ValidationCode.PROHIBITED_COMMAND, "unsupported GitHub command type"
        )

    def read_codeowners(self, ref: str) -> CodeownersFile:
        if not isinstance(ref, str) or not ref.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "ref must be non-empty text"
            )
        return CodeownersFile.parse(self._codeowners_content)

    def list_pull_request_files(self, pull_request_number: int) -> tuple[str, ...]:
        if pull_request_number not in self._pull_request_files:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"no recorded files for pull request {pull_request_number}",
            )
        return self._pull_request_files[pull_request_number]

    def get_branch_head(self, branch: str) -> TargetCommit:
        if not isinstance(branch, str) or not branch.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "branch must be non-empty text"
            )
        return self._branch_head

    def get_pull_request_head(self, pull_request_number: int) -> TargetCommit:
        try:
            return self._pull_request_heads[pull_request_number]
        except KeyError as error:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"no recorded head for pull request {pull_request_number}",
            ) from error

    def get_issue(self, issue_number: int) -> IssueSnapshot:
        try:
            return self._issues[issue_number]
        except KeyError as error:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"no recorded issue {issue_number}",
            ) from error

    @property
    def recorded(self) -> tuple[RecordedCommand, ...]:
        with self._lock:
            return tuple(self._recorded)

    @property
    def commands(self) -> tuple[GitHubCommand, ...]:
        return tuple(entry.command for entry in self.recorded)

    def commands_for(self, capability: GitHubCapability) -> tuple[GitHubCommand, ...]:
        return tuple(entry.command for entry in self.recorded if entry.capability is capability)


@dataclass
class LiveGitHubClient:
    """REST client for ``exloong/superset`` built on an injected transport."""

    transport: HttpTransport
    token_provider: TokenProvider
    allowed_capabilities: frozenset[GitHubCapability] = frozenset(GitHubCapability)
    api_root: str = GITHUB_API_ROOT
    correlation_id: str | None = None
    repository: RepositoryIdentity = SUPERSET_REPOSITORY

    def __post_init__(self) -> None:
        require_superset_repository(self.repository)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token_provider.token()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _url(self, path: str) -> str:
        return f"{self.api_root}/repos/{self.repository.full_name}{path}"

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        query: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self.transport.send(
            HttpRequest(
                method=method,
                url=self._url(path),
                headers=self._headers(),
                json_body=json_body,
                query=dict(query or {}),
                correlation_id=self.correlation_id,
            )
        )

    def execute(self, command: GitHubCommand) -> CommandResult:
        _authorize(command, self.allowed_capabilities)
        if isinstance(command, PostIssueComment):
            return self._post_comment(issue_number=command.issue_number, body=command.body)
        if isinstance(command, AddIssueLabels):
            response = self._send(
                "POST",
                f"/issues/{command.issue_number}/labels",
                json_body={"labels": list(command.labels)},
            )
            require_success(response, action="add labels")
            return LabelsApplied(
                issue_number=command.issue_number,
                labels=tuple(sorted(command.labels)),
            )
        if isinstance(command, CreateBranch):
            response = self._send(
                "POST",
                "/git/refs",
                json_body={
                    "ref": f"refs/heads/{command.branch_name}",
                    "sha": command.base_commit.sha,
                },
            )
            require_success(response, action="create branch")
            return BranchCreated(branch_name=command.branch_name, commit=command.base_commit)
        if isinstance(command, CreatePullRequest):
            payload = require_json_object(
                self._send(
                    "POST",
                    "/pulls",
                    json_body={
                        "title": command.title,
                        "body": command.body,
                        "head": command.head_branch,
                        "base": command.base_branch,
                        "draft": command.draft,
                        "maintainer_can_modify": True,
                    },
                ),
                action="create pull request",
            )
            number = _require_positive(
                require_int(payload, "number", action="create pull request"),
                "pull request number",
            )
            html_url = require_str(payload, "html_url", action="create pull request")
            validate_pull_request_url(html_url, pull_request_number=number)
            return PullRequestOpened(
                pull_request_number=number,
                html_url=html_url,
                head_branch=command.head_branch,
                base_branch=command.base_branch,
                draft=command.draft,
                source_issue_number=command.source_issue_number,
            )
        if isinstance(command, LinkPullRequestToIssue):
            posted = self._post_comment(
                issue_number=command.issue_number, body=command.comment_body
            )
            return PullRequestLinked(
                issue_number=command.issue_number,
                pull_request_number=command.pull_request_number,
                comment_id=posted.comment_id,
            )
        if isinstance(command, RequestReviewers):
            payload = require_json_object(
                self._send(
                    "POST",
                    f"/pulls/{command.pull_request_number}/requested_reviewers",
                    json_body={
                        "reviewers": list(command.reviewers),
                        "team_reviewers": list(command.team_reviewers),
                    },
                ),
                action="request reviewers",
            )
            accepted = _accepted_logins(payload.get("requested_reviewers"), "login")
            accepted_teams = _accepted_logins(payload.get("requested_teams"), "slug")
            _validate_pull_request_number(payload, command.pull_request_number)
            return ReviewersRequested(
                pull_request_number=command.pull_request_number,
                accepted_reviewers=accepted,
                accepted_team_reviewers=accepted_teams,
                rejected_reviewers=tuple(
                    login for login in command.reviewers if login not in accepted
                ),
            )
        raise ContractValidationError(  # pragma: no cover - defensive
            ValidationCode.PROHIBITED_COMMAND, "unsupported GitHub command type"
        )

    def _post_comment(self, *, issue_number: int, body: str) -> CommentPosted:
        payload = require_json_object(
            self._send(
                "POST",
                f"/issues/{issue_number}/comments",
                json_body={"body": body},
            ),
            action="post comment",
        )
        comment_id = _require_positive(
            require_int(payload, "id", action="post comment"), "comment id"
        )
        return CommentPosted(
            issue_number=issue_number,
            comment_id=comment_id,
            html_url=validate_issue_comment_url(
                require_str(payload, "html_url", action="post comment"),
                issue_number=issue_number,
                comment_id=comment_id,
            ),
        )

    def read_codeowners(self, ref: str) -> CodeownersFile:
        if not isinstance(ref, str) or not ref.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "ref must be non-empty text"
            )
        payload = require_json_object(
            self._send(
                "GET",
                f"/contents/{CODEOWNERS_PATH}",
                query={"ref": ref},
            ),
            action="read CODEOWNERS",
        )
        content = require_str(payload, "content", action="read CODEOWNERS")
        encoding = payload.get("encoding", "base64")
        if encoding != "base64":
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "CODEOWNERS content encoding is unsupported",
            )
        return CodeownersFile.parse(_decode_base64(content))

    def list_pull_request_files(self, pull_request_number: int) -> tuple[str, ...]:
        """List every changed path, paging deterministically to exhaustion.

        Reviewer routing depends on the complete changed-path set, so a
        truncated first page would silently misroute. Paging stops only when
        GitHub returns a short page; a pull request larger than the bound
        fails closed instead of returning a partial answer.
        """
        action = "list pull request files"
        _require_positive(pull_request_number, "pull request number")
        paths: list[str] = []
        seen: set[str] = set()
        for page in range(1, MAX_PULL_REQUEST_FILE_PAGES + 1):
            entries = object_array(
                require_json_array(
                    self._send(
                        "GET",
                        f"/pulls/{pull_request_number}/files",
                        query={
                            "per_page": str(PULL_REQUEST_FILE_PAGE_SIZE),
                            "page": str(page),
                        },
                    ),
                    action=action,
                ),
                action=action,
            )
            if len(entries) > PULL_REQUEST_FILE_PAGE_SIZE:
                raise ContractValidationError(
                    ValidationCode.MALFORMED_RESPONSE,
                    "pull request file page exceeded the requested page size",
                )
            for entry in entries:
                filename = require_str(entry, "filename", action=action)
                if filename in seen:
                    raise ContractValidationError(
                        ValidationCode.MALFORMED_RESPONSE,
                        "pull request file pagination repeated a path",
                    )
                seen.add(filename)
                paths.append(filename)
            if len(entries) < PULL_REQUEST_FILE_PAGE_SIZE:
                return tuple(paths)
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"pull request changed more than {MAX_PULL_REQUEST_FILES} files",
        )

    def get_branch_head(self, branch: str) -> TargetCommit:
        if not isinstance(branch, str) or not branch.strip():
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "branch must be non-empty text"
            )
        payload = require_json_object(
            self._send("GET", f"/commits/{branch.strip()}"),
            action="resolve branch head",
        )
        return TargetCommit(sha=require_str(payload, "sha", action="resolve branch head"))

    def get_pull_request_head(self, pull_request_number: int) -> TargetCommit:
        _require_positive(pull_request_number, "pull request number")
        payload = require_json_object(
            self._send("GET", f"/pulls/{pull_request_number}"),
            action="resolve pull request head",
        )
        _validate_pull_request_number(payload, pull_request_number)
        head = payload.get("head")
        if not isinstance(head, Mapping):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "pull request response has no head object",
            )
        return TargetCommit(sha=require_str(head, "sha", action="resolve pull request head"))

    def get_issue(self, issue_number: int) -> IssueSnapshot:
        _require_positive(issue_number, "issue number")
        action = "read issue"
        payload = require_json_object(self._send("GET", f"/issues/{issue_number}"), action=action)
        if require_int(payload, "number", action=action) != issue_number:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "response issue number does not match the request",
            )
        if isinstance(payload.get("pull_request"), Mapping):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, f"#{issue_number} is a pull request"
            )
        body = payload.get("body")
        labels: list[str] = []
        for label in object_array(payload.get("labels"), action=action):
            name = label.get("name")
            if isinstance(name, str) and name:
                labels.append(name)
        user = payload.get("user")
        reporter = user.get("login") if isinstance(user, Mapping) else None
        return IssueSnapshot(
            number=issue_number,
            title=require_str(payload, "title", action=action),
            body=body if isinstance(body, str) else "",
            state=require_str(payload, "state", action=action),
            labels=tuple(labels),
            reporter_login=reporter if isinstance(reporter, str) and reporter else "unknown",
        )


def _require_positive(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} must be a positive integer",
        )
    return value


def _validate_pull_request_number(payload: JsonObject, expected: int) -> None:
    value = payload.get("number")
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "response pull request number does not match the request",
        )


class StaticTokenProvider:
    """Wraps an already-resolved token; the value is never logged."""

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be non-empty text")
        self._token = token

    def token(self) -> str:
        return self._token

    def __repr__(self) -> str:
        return "StaticTokenProvider(token='[redacted]')"


def _accepted_logins(value: JsonValue, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    entries = object_array(value, action="reviewer response")
    logins: list[str] = []
    for entry in entries:
        login = entry.get(key)
        if not isinstance(login, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "reviewer response entry is malformed",
            )
        logins.append(login)
    return tuple(logins)


def _decode_base64(content: str) -> str:
    try:
        return b64decode(content, validate=False).decode("utf-8")
    except (BinasciiError, UnicodeDecodeError, ValueError) as error:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "CODEOWNERS content is not valid base64 UTF-8",
        ) from error
