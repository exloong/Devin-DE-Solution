"""The single repository Relay is allowed to read from and write to."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import ContractValidationError, ValidationCode

SUPERSET_OWNER = "exloong"
SUPERSET_NAME = "superset"
SUPERSET_FULL_NAME = f"{SUPERSET_OWNER}/{SUPERSET_NAME}"
SUPERSET_HTML_ROOT = f"https://github.com/{SUPERSET_FULL_NAME}"

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,199})$")


@dataclass(frozen=True, order=True)
class RepositoryIdentity:
    """A normalized ``owner/name`` GitHub repository reference."""

    owner: str
    name: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.owner, str)
            or not _OWNER_PATTERN.fullmatch(self.owner)
            or self.owner != self.owner.lower()
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "repository owner is malformed"
            )
        if (
            not isinstance(self.name, str)
            or not _NAME_PATTERN.fullmatch(self.name)
            or self.name != self.name.lower()
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "repository name is malformed"
            )

    @classmethod
    def normalized(cls, owner: object, name: object) -> RepositoryIdentity:
        """Build an identity from case-insensitive GitHub input."""
        if not isinstance(owner, str) or not isinstance(name, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository owner and name must be text",
            )
        return cls(owner=owner.lower(), name=name.lower())

    @classmethod
    def from_full_name(cls, full_name: object) -> RepositoryIdentity:
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository full name must use owner/name",
            )
        owner, name = full_name.split("/", maxsplit=1)
        return cls.normalized(owner, name)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def __str__(self) -> str:
        return self.full_name


SUPERSET_REPOSITORY = RepositoryIdentity(owner=SUPERSET_OWNER, name=SUPERSET_NAME)


def require_superset_repository(repository: object) -> RepositoryIdentity:
    """Return the Superset identity or reject any other repository target."""
    if isinstance(repository, str):
        candidate = RepositoryIdentity.from_full_name(repository)
    elif isinstance(repository, RepositoryIdentity):
        candidate = repository
    else:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            "repository must be a full name or RepositoryIdentity",
        )
    if candidate != SUPERSET_REPOSITORY:
        raise ContractValidationError(
            ValidationCode.UNAUTHORIZED_REPOSITORY,
            f"repository {candidate.full_name} is not {SUPERSET_FULL_NAME}",
        )
    return SUPERSET_REPOSITORY


def validate_superset_repository_field(repository: object) -> None:
    """Validate an already-normalized Superset repository field.

    Command and envelope records keep their repository immutable, so the value
    must arrive as the normalized identity instead of being rewritten after
    construction.
    """
    identity = require_superset_repository(repository)
    if repository != identity:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            "repository must be the normalized Superset RepositoryIdentity",
        )


class TargetCommit:
    """An immutable Superset commit that bounds one agent session.

    The SHA is normalized by the constructor, so every instance is already
    lowercase and no attribute is ever rewritten afterwards.
    """

    __slots__ = ("_sha",)

    def __init__(self, sha: object) -> None:
        if not isinstance(sha, str) or not _SHA_PATTERN.fullmatch(sha.lower()):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "target commit must be a full 40 character SHA",
            )
        self._sha = sha.lower()

    @property
    def sha(self) -> str:
        return self._sha

    @property
    def short_sha(self) -> str:
        return self._sha[:12]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TargetCommit):
            return NotImplemented
        return self._sha == other._sha

    def __hash__(self) -> int:
        return hash((TargetCommit, self._sha))

    def __repr__(self) -> str:
        return f"TargetCommit(sha={self._sha!r})"

    def __str__(self) -> str:
        return self._sha


def _superset_url_parts(url: object, *, action: str) -> tuple[tuple[str, ...], str]:
    """Split a GitHub URL after asserting that it addresses Superset.

    A URL returned by an API is untrusted input: it decides which repository a
    reader is sent to, so the host, owner, and name are checked exactly before
    the URL is exposed anywhere.
    """
    if not isinstance(url, str):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, f"{action} URL must be text"
        )
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query:
        raise ContractValidationError(
            ValidationCode.UNAUTHORIZED_REPOSITORY,
            f"{action} URL is not a github.com URL",
        )
    segments = tuple(segment for segment in parsed.path.split("/") if segment)
    if segments[:2] != (SUPERSET_OWNER, SUPERSET_NAME):
        raise ContractValidationError(
            ValidationCode.UNAUTHORIZED_REPOSITORY,
            f"{action} URL does not target {SUPERSET_FULL_NAME}",
        )
    return segments[2:], parsed.fragment


def _positive_number(value: str, *, action: str) -> int:
    if not value.isdigit() or value.startswith("0"):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} URL has a malformed number",
        )
    return int(value)


def superset_pull_request_url(pull_request_number: int) -> str:
    """Build the canonical Superset pull-request URL."""
    if isinstance(pull_request_number, bool) or (
        not isinstance(pull_request_number, int) or pull_request_number < 1
    ):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            "pull request number must be a positive integer",
        )
    return f"{SUPERSET_HTML_ROOT}/pull/{pull_request_number}"


def validate_pull_request_url(
    url: object, *, pull_request_number: int | None = None
) -> int:
    """Return the pull-request number named by a validated Superset URL."""
    action = "pull request"
    segments, fragment = _superset_url_parts(url, action=action)
    if fragment or len(segments) != 2 or segments[0] != "pull":
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "pull request URL is not a Superset pull-request URL",
        )
    number = _positive_number(segments[1], action=action)
    if pull_request_number is not None and number != pull_request_number:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "pull request URL does not address the expected pull request",
        )
    return number


def validate_issue_comment_url(
    url: object, *, issue_number: int, comment_id: int
) -> str:
    """Return ``url`` only if it addresses the created Superset comment."""
    action = "issue comment"
    segments, fragment = _superset_url_parts(url, action=action)
    fragment_form = segments == ("issues", str(issue_number)) and (
        fragment == f"issuecomment-{comment_id}"
    )
    path_form = not fragment and segments == (
        "issues",
        str(issue_number),
        "comments",
        str(comment_id),
    )
    if not fragment_form and not path_form:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "issue comment URL does not address the created comment",
        )
    if not isinstance(url, str):  # pragma: no cover - narrowed above
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, "issue comment URL must be text"
        )
    return url


def validate_branch_name(branch_name: object) -> str:
    """Validate a Git branch name used for a Relay-created fix branch."""
    if (
        not isinstance(branch_name, str)
        or not _BRANCH_PATTERN.fullmatch(branch_name)
        or ".." in branch_name
        or "//" in branch_name
        or branch_name.startswith(".")
        or branch_name.endswith(("/", ".", ".lock"))
        or "@{" in branch_name
    ):
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE, "branch name is malformed"
        )
    return branch_name
