"""Injected HTTP transport boundary shared by live integration clients.

Live clients never construct a network session themselves. They send typed
requests through an injected :class:`HttpTransport`, so tests can supply a
deterministic transport and require neither network access nor credentials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .errors import ContractValidationError, ValidationCode
from .redaction import redact_mapping, redact_text

_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})


@dataclass(frozen=True)
class HttpRequest:
    """One outbound API call described independently of any HTTP library."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: Mapping[str, Any] | None = None
    query: Mapping[str, str] = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if self.method not in _ALLOWED_METHODS:
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                f"HTTP method {self.method!r} is not supported",
            )
        if not isinstance(self.url, str) or not self.url.startswith("https://"):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "request URL must be an https URL",
            )

    @property
    def redacted(self) -> Mapping[str, Any]:
        """A retention-safe view of this request for audit records."""
        return {
            "method": self.method,
            "url": redact_text(self.url),
            "headers": redact_mapping(dict(self.headers)),
            "query": redact_mapping(dict(self.query)),
            "json_body": (
                None if self.json_body is None else redact_mapping(dict(self.json_body))
            ),
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class HttpResponse:
    """A transport-agnostic response with retained, redactable metadata."""

    status_code: int
    json_body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "response status code is invalid"
            )

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def redacted_metadata(self) -> Mapping[str, Any]:
        return {
            "status_code": self.status_code,
            "headers": redact_mapping(dict(self.headers)),
            "correlation_id": self.correlation_id,
        }


class HttpTransport(Protocol):
    """Sends a typed request and returns a typed response."""

    def send(self, request: HttpRequest) -> HttpResponse:
        """Perform ``request`` and return its response."""


class RecordedTransport:
    """Deterministic transport that replays scripted responses in order."""

    def __init__(self, responses: Sequence[HttpResponse] = ()) -> None:
        self._responses: list[HttpResponse] = list(responses)
        self._requests: list[HttpRequest] = []

    def enqueue(self, response: HttpResponse) -> None:
        self._responses.append(response)

    def send(self, request: HttpRequest) -> HttpResponse:
        self._requests.append(request)
        if not self._responses:
            raise ContractValidationError(
                ValidationCode.TRANSPORT_FAILURE,
                "no scripted response remains for this request",
            )
        return self._responses.pop(0)

    @property
    def requests(self) -> tuple[HttpRequest, ...]:
        return tuple(self._requests)

    @property
    def redacted_requests(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(request.redacted for request in self._requests)


def require_success(response: HttpResponse, *, action: str) -> HttpResponse:
    """Reject a non-success response with a stable validation code."""
    if not response.is_success:
        raise ContractValidationError(
            ValidationCode.TRANSPORT_FAILURE,
            f"{action} failed with status {response.status_code}",
        )
    return response


def require_json_object(response: HttpResponse, *, action: str) -> Mapping[str, Any]:
    """Require a JSON object body from a successful response."""
    require_success(response, action=action)
    if not isinstance(response.json_body, Mapping):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} did not return a JSON object",
        )
    return response.json_body


def require_json_array(response: HttpResponse, *, action: str) -> Sequence[Any]:
    """Require a JSON array body from a successful response."""
    require_success(response, action=action)
    body = response.json_body
    if isinstance(body, Mapping) or not isinstance(body, Sequence):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} did not return a JSON array",
        )
    return body


def require_field(
    payload: Mapping[str, Any],
    key: str,
    expected: type | tuple[type, ...],
    *,
    action: str,
) -> Any:
    """Read a required, correctly typed field from a response payload."""
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    value = payload.get(key)
    accepted = isinstance(value, expected_types)
    if bool not in expected_types and isinstance(value, bool):
        accepted = False
    if not accepted:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is missing or malformed",
        )
    return value


def parse_timestamp(
    value: object, field_name: str, *, default: datetime | None = None
) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp from a response payload."""
    if value is None:
        if default is not None:
            return default
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, f"{field_name} is missing"
        )
    if not isinstance(value, str):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} must be an ISO-8601 timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} is not a valid timestamp",
        ) from error
    if parsed.tzinfo is None:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} must include a timezone",
        )
    return parsed
