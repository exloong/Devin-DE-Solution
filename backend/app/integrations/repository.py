"""The single repository Relay is allowed to read from and write to."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ContractValidationError, ValidationCode

SUPERSET_OWNER = "exloong"
SUPERSET_NAME = "superset"
SUPERSET_FULL_NAME = f"{SUPERSET_OWNER}/{SUPERSET_NAME}"

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
        if not isinstance(self.owner, str) or not _OWNER_PATTERN.fullmatch(self.owner):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "repository owner is malformed"
            )
        if not isinstance(self.name, str) or not _NAME_PATTERN.fullmatch(self.name):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "repository name is malformed"
            )
        object.__setattr__(self, "owner", self.owner.lower())
        object.__setattr__(self, "name", self.name.lower())

    @classmethod
    def from_full_name(cls, full_name: object) -> RepositoryIdentity:
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository full name must use owner/name",
            )
        owner, name = full_name.split("/", maxsplit=1)
        return cls(owner=owner, name=name)

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


@dataclass(frozen=True)
class TargetCommit:
    """An immutable Superset commit that bounds one agent session."""

    sha: str

    def __post_init__(self) -> None:
        if not isinstance(self.sha, str) or not _SHA_PATTERN.fullmatch(
            self.sha.lower()
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "target commit must be a full 40 character SHA",
            )
        object.__setattr__(self, "sha", self.sha.lower())

    @property
    def short_sha(self) -> str:
        return self.sha[:12]

    def __str__(self) -> str:
        return self.sha


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
