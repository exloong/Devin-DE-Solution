"""GitHub webhook ingress: signature, deduplication, repository validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import Any, Protocol

from .errors import ContractValidationError, ValidationCode
from .redaction import redact_mapping
from .repository import (
    SUPERSET_FULL_NAME,
    RepositoryIdentity,
    require_superset_repository,
)

_DELIVERY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SIGNATURE_PATTERN = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_MAX_BODY_BYTES = 8 * 1024 * 1024


class GitHubEventName(str, Enum):
    """Webhook event types Relay accepts from ``exloong/superset``."""

    ISSUES = "issues"
    ISSUE_COMMENT = "issue_comment"
    PULL_REQUEST = "pull_request"
    PULL_REQUEST_REVIEW = "pull_request_review"
    CHECK_SUITE = "check_suite"

    @classmethod
    def parse(cls, value: object) -> GitHubEventName:
        try:
            return cls(value)
        except ValueError as error:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                f"GitHub event {value!r} is not accepted by Relay",
            ) from error


@dataclass(frozen=True, order=True)
class DeliveryKey:
    """Deduplication key for one inbound webhook delivery."""

    source: str
    delivery_id: str

    def __post_init__(self) -> None:
        if self.source != "github":
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "unsupported delivery source"
            )
        if not isinstance(self.delivery_id, str) or not _DELIVERY_ID_PATTERN.fullmatch(
            self.delivery_id
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "GitHub delivery ID is malformed"
            )

    def __str__(self) -> str:
        return f"{self.source}:{self.delivery_id}"


def verify_sha256_signature(
    *,
    secret: bytes,
    raw_body: bytes,
    signature_header: str | None,
) -> bool:
    """Constant-time raw-body HMAC-SHA256 verification of a GitHub signature."""
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be non-empty bytes")
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")

    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    supplied = "0" * 64
    if isinstance(signature_header, str):
        match = _SIGNATURE_PATTERN.fullmatch(signature_header.strip())
        if match is not None:
            supplied = match.group(1).lower()
    return hmac.compare_digest(expected, supplied)


@dataclass(frozen=True)
class GitHubWebhookEnvelope:
    """A raw, unverified webhook delivery preserved exactly as received."""

    delivery: DeliveryKey
    event_name: str
    signature_header: str
    raw_body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.event_name, str) or not 1 <= len(self.event_name) <= 64:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "event name is malformed"
            )
        if not isinstance(self.signature_header, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "signature header must be text"
            )
        if not isinstance(self.raw_body, bytes):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "raw body must be bytes"
            )
        if len(self.raw_body) > _MAX_BODY_BYTES:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "raw body exceeds the size limit"
            )

    @classmethod
    def from_request(
        cls,
        *,
        headers: Mapping[str, str],
        raw_body: bytes,
    ) -> GitHubWebhookEnvelope:
        normalized = {str(key).lower(): value for key, value in headers.items()}
        return cls(
            delivery=DeliveryKey(
                source="github",
                delivery_id=normalized.get("x-github-delivery", ""),
            ),
            event_name=normalized.get("x-github-event", ""),
            signature_header=normalized.get("x-hub-signature-256", ""),
            raw_body=raw_body,
        )


@dataclass(frozen=True)
class AcceptedWebhookEvent:
    """A verified Superset webhook delivery ready for lifecycle processing."""

    delivery: DeliveryKey
    event_name: GitHubEventName
    action: str | None
    repository: RepositoryIdentity
    issue_number: int | None
    pull_request_number: int | None
    redacted_payload: Mapping[str, Any]


class DeliveryDeduplicator(Protocol):
    """Records delivery keys so a repeated delivery cannot be processed twice."""

    def record_once(self, delivery: DeliveryKey) -> bool:
        """Atomically record ``delivery`` and report whether it was new."""


class InMemoryDeliveryDeduplicator:
    """Thread-safe deduplicator used by tests and local dry runs."""

    def __init__(self) -> None:
        self._deliveries: set[DeliveryKey] = set()
        self._lock = Lock()

    def record_once(self, delivery: DeliveryKey) -> bool:
        with self._lock:
            if delivery in self._deliveries:
                return False
            self._deliveries.add(delivery)
            return True

    def seen(self, delivery: DeliveryKey) -> bool:
        with self._lock:
            return delivery in self._deliveries

    def __len__(self) -> int:
        with self._lock:
            return len(self._deliveries)


def _optional_number(container: object, key: str) -> int | None:
    if not isinstance(container, Mapping):
        return None
    value = container.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


@dataclass
class GitHubWebhookVerifier:
    """Verifies signature, event type, repository, and delivery uniqueness."""

    secret: bytes
    deduplicator: DeliveryDeduplicator = field(
        default_factory=InMemoryDeliveryDeduplicator
    )

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes) or not self.secret:
            raise ValueError("secret must be non-empty bytes")

    def accept(self, envelope: GitHubWebhookEnvelope) -> AcceptedWebhookEvent:
        """Validate one delivery and return its typed, redacted event."""
        if not verify_sha256_signature(
            secret=self.secret,
            raw_body=envelope.raw_body,
            signature_header=envelope.signature_header,
        ):
            raise ContractValidationError(
                ValidationCode.INVALID_SIGNATURE,
                "GitHub webhook signature is invalid",
            )

        event_name = GitHubEventName.parse(envelope.event_name)
        payload = _parse_json_object(envelope.raw_body)
        repository_payload = payload.get("repository")
        if not isinstance(repository_payload, Mapping):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "GitHub webhook repository is missing",
            )
        repository = require_superset_repository(
            RepositoryIdentity.from_full_name(repository_payload.get("full_name"))
        )

        action = payload.get("action")
        if action is not None and not isinstance(action, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE, "GitHub action must be text"
            )

        if not self.deduplicator.record_once(envelope.delivery):
            raise ContractValidationError(
                ValidationCode.DUPLICATE_DELIVERY,
                f"delivery {envelope.delivery} was already recorded",
            )

        return AcceptedWebhookEvent(
            delivery=envelope.delivery,
            event_name=event_name,
            action=action,
            repository=repository,
            issue_number=_optional_number(payload.get("issue"), "number"),
            pull_request_number=_optional_number(payload.get("pull_request"), "number"),
            redacted_payload=redact_mapping(payload),
        )


def _parse_json_object(raw_body: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw_body)
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
    return payload


__all__ = [
    "SUPERSET_FULL_NAME",
    "AcceptedWebhookEvent",
    "DeliveryDeduplicator",
    "DeliveryKey",
    "GitHubEventName",
    "GitHubWebhookEnvelope",
    "GitHubWebhookVerifier",
    "InMemoryDeliveryDeduplicator",
    "verify_sha256_signature",
]
