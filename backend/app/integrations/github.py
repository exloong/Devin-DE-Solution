"""GitHub ingress validation and dry-run egress command contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from types import MappingProxyType
from typing import Mapping, Protocol, TypeAlias

from .errors import ContractValidationError, ValidationCode

_DELIVERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_LABEL_PATTERN = re.compile(r"^[^\x00-\x1f\x7f]{1,50}$")
_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,199})$")
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-fA-F]{64})$")


def _require_nonempty(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ContractValidationError(
            ValidationCode.MALFORMED_ENVELOPE,
            f"{field_name} must contain 1 to {max_length} characters",
        )
    return value


@dataclass(frozen=True, slots=True, order=True)
class RepositoryIdentity:
    owner: str
    name: str

    def __post_init__(self) -> None:
        if not _OWNER_PATTERN.fullmatch(self.owner):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository owner is malformed",
            )
        if not _REPOSITORY_PATTERN.fullmatch(self.name):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository name is malformed",
            )
        object.__setattr__(self, "owner", self.owner.lower())
        object.__setattr__(self, "name", self.name.lower())

    @classmethod
    def from_full_name(cls, full_name: str) -> RepositoryIdentity:
        if not isinstance(full_name, str) or full_name.count("/") != 1:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "repository full name must use owner/name",
            )
        owner, name = full_name.split("/", maxsplit=1)
        return cls(owner=owner.lower(), name=name.lower())

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class DeliveryIdentity:
    source: str
    delivery_id: str

    def __post_init__(self) -> None:
        if self.source != "github":
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "unsupported delivery source",
            )
        if not _DELIVERY_ID_PATTERN.fullmatch(self.delivery_id):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "GitHub delivery ID is malformed",
            )


@dataclass(frozen=True, slots=True)
class GitHubWebhookEnvelope:
    delivery: DeliveryIdentity
    event_name: str
    signature: str
    raw_body: bytes

    def __post_init__(self) -> None:
        _require_nonempty(self.event_name, "event_name", 64)
        if not isinstance(self.signature, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "signature must be text",
            )
        if not isinstance(self.raw_body, bytes):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "raw_body must be bytes",
            )

    @classmethod
    def from_headers(
        cls,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> GitHubWebhookEnvelope:
        normalized = {key.lower(): value for key, value in headers.items()}
        delivery_id = normalized.get("x-github-delivery", "")
        event_name = normalized.get("x-github-event", "")
        signature = normalized.get("x-hub-signature-256", "")
        return cls(
            delivery=DeliveryIdentity(source="github", delivery_id=delivery_id),
            event_name=event_name,
            signature=signature,
            raw_body=raw_body,
        )

    def verified_payload(self, secret: bytes) -> Mapping[str, object]:
        if not verify_sha256_signature(
            secret=secret,
            raw_body=self.raw_body,
            signature_header=self.signature,
        ):
            raise ContractValidationError(
                ValidationCode.INVALID_SIGNATURE,
                "GitHub webhook signature is invalid",
            )
        try:
            payload = json.loads(self.raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "GitHub webhook body is not valid JSON",
            ) from error
        if not isinstance(payload, dict):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "GitHub webhook body must be a JSON object",
            )
        return MappingProxyType(payload)

    def verified_repository(
        self,
        *,
        secret: bytes,
        allowlist: RepositoryAllowlist,
    ) -> RepositoryIdentity:
        payload = self.verified_payload(secret)
        repository = payload.get("repository")
        if not isinstance(repository, dict):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "GitHub webhook repository is missing",
            )
        identity = RepositoryIdentity.from_full_name(repository.get("full_name", ""))
        allowlist.require(identity)
        return identity


def verify_sha256_signature(
    *,
    secret: bytes,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")

    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    supplied = "0" * 64
    if isinstance(signature_header, str):
        match = _SIGNATURE_PATTERN.fullmatch(signature_header)
        if match is not None:
            supplied = match.group(1).lower()
    return hmac.compare_digest(expected, supplied)


class RepositoryAllowlist:
    def __init__(self, repositories: set[RepositoryIdentity] | frozenset[RepositoryIdentity]):
        self._repositories = frozenset(repositories)

    def allows(self, repository: RepositoryIdentity) -> bool:
        return repository in self._repositories

    def require(self, repository: RepositoryIdentity) -> None:
        if not self.allows(repository):
            raise ContractValidationError(
                ValidationCode.UNAUTHORIZED_REPOSITORY,
                f"repository {repository.full_name} is not allowlisted",
            )


class DeliveryDeduplicator(Protocol):
    def record_once(self, delivery: DeliveryIdentity) -> bool:
        """Atomically record a delivery and return whether it was new."""


class InMemoryDeliveryDeduplicator:
    def __init__(self) -> None:
        self._deliveries: set[DeliveryIdentity] = set()
        self._lock = Lock()

    def record_once(self, delivery: DeliveryIdentity) -> bool:
        with self._lock:
            if delivery in self._deliveries:
                return False
            self._deliveries.add(delivery)
            return True

    def require_new(self, delivery: DeliveryIdentity) -> None:
        if not self.record_once(delivery):
            raise ContractValidationError(
                ValidationCode.DUPLICATE_DELIVERY,
                "GitHub delivery was already recorded",
            )

    def __len__(self) -> int:
        with self._lock:
            return len(self._deliveries)


class GitHubCapability(str, Enum):
    COMMENT = "comment"
    LABEL = "label"
    BRANCH = "branch"
    PULL_REQUEST_DRAFT = "pull_request_draft"


@dataclass(frozen=True, slots=True)
class PostIssueComment:
    repository: RepositoryIdentity
    issue_number: int
    body: str

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        _require_nonempty(self.body, "body", 65_536)

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.COMMENT


@dataclass(frozen=True, slots=True)
class AddLabels:
    repository: RepositoryIdentity
    issue_number: int
    labels: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.issue_number < 1:
            raise ValueError("issue_number must be positive")
        if not self.labels or len(self.labels) > 20:
            raise ValueError("labels must contain 1 to 20 values")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be unique")
        if any(not _LABEL_PATTERN.fullmatch(label) for label in self.labels):
            raise ValueError("label is malformed")

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.LABEL


def _validate_branch_name(branch_name: str) -> None:
    if (
        not _BRANCH_PATTERN.fullmatch(branch_name)
        or ".." in branch_name
        or "//" in branch_name
        or branch_name.endswith(("/", ".", ".lock"))
        or branch_name.startswith(".")
    ):
        raise ValueError("branch name is malformed")


@dataclass(frozen=True, slots=True)
class CreateBranch:
    repository: RepositoryIdentity
    branch_name: str
    base_sha: str

    def __post_init__(self) -> None:
        _validate_branch_name(self.branch_name)
        if not _SHA_PATTERN.fullmatch(self.base_sha):
            raise ValueError("base_sha is malformed")

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.BRANCH


@dataclass(frozen=True, slots=True)
class CreateDraftPullRequest:
    repository: RepositoryIdentity
    head_branch: str
    base_branch: str
    title: str
    body: str

    def __post_init__(self) -> None:
        _validate_branch_name(self.head_branch)
        _validate_branch_name(self.base_branch)
        _require_nonempty(self.title, "title", 256)
        if not isinstance(self.body, str) or len(self.body) > 65_536:
            raise ValueError("body must be text no longer than 65536 characters")
        if self.head_branch == self.base_branch:
            raise ValueError("head_branch and base_branch must differ")

    @property
    def capability(self) -> GitHubCapability:
        return GitHubCapability.PULL_REQUEST_DRAFT


GitHubDryRunCommand: TypeAlias = (
    PostIssueComment | AddLabels | CreateBranch | CreateDraftPullRequest
)
_COMMAND_TYPES = (PostIssueComment, AddLabels, CreateBranch, CreateDraftPullRequest)


class FakeGitHubAdapter:
    def __init__(
        self,
        allowed_capabilities: frozenset[GitHubCapability] | None = None,
    ) -> None:
        self._allowed_capabilities = (
            frozenset(GitHubCapability)
            if allowed_capabilities is None
            else allowed_capabilities
        )
        self._commands: list[GitHubDryRunCommand] = []
        self._lock = Lock()

    def execute(self, command: GitHubDryRunCommand) -> None:
        if not isinstance(command, _COMMAND_TYPES):
            raise ContractValidationError(
                ValidationCode.PROHIBITED_COMMAND,
                "unsupported GitHub command type",
            )
        if command.capability not in self._allowed_capabilities:
            raise ContractValidationError(
                ValidationCode.PROHIBITED_CAPABILITY,
                f"GitHub capability {command.capability} is not allowed",
            )
        with self._lock:
            self._commands.append(command)

    @property
    def commands(self) -> tuple[GitHubDryRunCommand, ...]:
        with self._lock:
            return tuple(self._commands)
