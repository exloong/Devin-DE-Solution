"""Injected HTTP transport boundary shared by live integration clients.

Live clients never construct a network session themselves. They send typed
requests through an injected :class:`HttpTransport`, so tests can supply a
deterministic transport and require neither network access nor credentials.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

import httpx

from .errors import ContractValidationError, ValidationCode
from .json_values import JsonArray, JsonObject, JsonValue
from .redaction import redact_mapping, redact_text

_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH", "PUT", "DELETE"})
_MAX_UNIX_TIMESTAMP = 4_102_444_800


class TokenProvider(Protocol):
    """Supplies a short-lived credential from runtime secret management."""

    def token(self) -> str:
        """Return the current bearer token."""


@dataclass(frozen=True)
class HttpRequest:
    """One outbound API call described independently of any HTTP library."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: JsonObject | None = None
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
    def redacted(self) -> JsonObject:
        """A retention-safe view of this request for audit records."""
        return {
            "method": self.method,
            "url": redact_text(self.url),
            "headers": redact_mapping(dict(self.headers)),
            "query": redact_mapping(dict(self.query)),
            "json_body": (
                None if self.json_body is None else redact_mapping(self.json_body)
            ),
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True)
class HttpResponse:
    """A transport-agnostic response with retained, redactable metadata."""

    status_code: int
    json_body: JsonValue = None
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
    def redacted_metadata(self) -> JsonObject:
        return {
            "status_code": self.status_code,
            "headers": redact_mapping(dict(self.headers)),
            "correlation_id": self.correlation_id,
        }


class HttpTransport(Protocol):
    """Sends a typed request and returns a typed response."""

    def send(self, request: HttpRequest) -> HttpResponse:
        """Perform ``request`` and return its response."""


@dataclass
class HttpxTransport:
    """Production HTTPS transport with bounded timeouts and retries."""

    timeout_seconds: float = 30.0
    retries: int = 2
    user_agent: str = "relay-control-plane/0.1"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")

    def send(self, request: HttpRequest) -> HttpResponse:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        headers.update(request.headers)
        if request.correlation_id:
            headers["X-Relay-Correlation-ID"] = request.correlation_id
        last_error: httpx.HTTPError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = httpx.request(
                    request.method,
                    request.url,
                    headers=headers,
                    params=request.query,
                    json=request.json_body,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                )
                try:
                    json_body = response.json() if response.content else None
                except ValueError:
                    json_body = response.text
                return HttpResponse(
                    status_code=response.status_code,
                    json_body=json_body,
                    headers=dict(response.headers),
                    correlation_id=request.correlation_id,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as error:
                last_error = error
                if attempt == self.retries:
                    break
        if last_error is None:
            raise RuntimeError("HTTP transport failed without an error")
        raise ContractValidationError(
            ValidationCode.TRANSPORT_FAILURE,
            f"upstream request failed after {self.retries + 1} attempts",
        ) from last_error


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
    def redacted_requests(self) -> tuple[JsonObject, ...]:
        return tuple(request.redacted for request in self._requests)


def require_success(response: HttpResponse, *, action: str) -> HttpResponse:
    """Reject a non-success response with a stable validation code."""
    if not response.is_success:
        raise ContractValidationError(
            ValidationCode.TRANSPORT_FAILURE,
            f"{action} failed with status {response.status_code}",
        )
    return response


def require_json_object(response: HttpResponse, *, action: str) -> JsonObject:
    """Require a JSON object body from a successful response."""
    require_success(response, action=action)
    if not isinstance(response.json_body, Mapping):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} did not return a JSON object",
        )
    return response.json_body


def require_json_array(response: HttpResponse, *, action: str) -> JsonArray:
    """Require a JSON array body from a successful response."""
    require_success(response, action=action)
    body = response.json_body
    if isinstance(body, (str, Mapping)) or not isinstance(body, Sequence):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} did not return a JSON array",
        )
    return body


def require_field(
    payload: JsonObject,
    key: str,
    expected: type | tuple[type, ...],
    *,
    action: str,
) -> JsonValue:
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


def require_str(payload: JsonObject, key: str, *, action: str) -> str:
    """Read a required string field."""
    value = require_field(payload, key, str, action=action)
    if not isinstance(value, str):  # pragma: no cover - defensive
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not text",
        )
    return value


def require_int(payload: JsonObject, key: str, *, action: str) -> int:
    """Read a required integer field, rejecting booleans."""
    value = require_field(payload, key, int, action=action)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractValidationError(  # pragma: no cover - defensive
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not an integer",
        )
    return value


def require_number(payload: JsonObject, key: str, *, action: str) -> float:
    """Read a required numeric field, rejecting booleans."""
    value = require_field(payload, key, (int, float), action=action)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractValidationError(  # pragma: no cover - defensive
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not a number",
        )
    return float(value)


def require_bool(payload: JsonObject, key: str, *, action: str) -> bool:
    """Read a required boolean field."""
    value = require_field(payload, key, bool, action=action)
    if not isinstance(value, bool):  # pragma: no cover - defensive
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not a boolean",
        )
    return value


def require_object(payload: JsonObject, key: str, *, action: str) -> JsonObject:
    """Read a required nested JSON object field."""
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not a JSON object",
        )
    return value


def require_array(payload: JsonObject, key: str, *, action: str) -> JsonArray:
    """Read a required nested JSON array field."""
    value = payload.get(key)
    if isinstance(value, (str, Mapping)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not a JSON array",
        )
    return value


def optional_str(payload: JsonObject, key: str, *, action: str) -> str | None:
    """Read an optional string field, rejecting a non-string value."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not text",
        )
    return value


def optional_object(payload: JsonObject, key: str, *, action: str) -> JsonObject | None:
    """Read an optional nested JSON object field."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{action} response field {key!r} is not a JSON object",
        )
    return value


def string_tuple(value: JsonValue, *, action: str) -> tuple[str, ...]:
    """Narrow an optional JSON array of strings."""
    if value is None:
        return ()
    if isinstance(value, (str, Mapping)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, f"{action} expected a list of strings"
        )
    strings: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"{action} expected a list of strings",
            )
        strings.append(entry)
    return tuple(strings)


def object_array(value: JsonValue, *, action: str) -> tuple[JsonObject, ...]:
    """Narrow a JSON array whose entries must all be objects."""
    if value is None:
        return ()
    if isinstance(value, (str, Mapping)) or not isinstance(value, Sequence):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, f"{action} expected a list of objects"
        )
    entries: list[JsonObject] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"{action} expected a list of objects",
            )
        entries.append(entry)
    return tuple(entries)


def parse_timestamp(
    value: JsonValue | datetime, field_name: str, *, default: datetime | None = None
) -> datetime:
    """Parse a timezone-aware timestamp from a response payload.

    Devin v3 returns Unix seconds while GitHub returns ISO-8601 text, so both
    encodings are accepted; a naive ISO timestamp is rejected rather than
    assumed to be UTC.
    """
    if value is None:
        if default is not None:
            return default
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, f"{field_name} is missing"
        )
    if isinstance(value, (bool, float)):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} must be Unix seconds or an ISO-8601 timestamp",
        )
    if isinstance(value, int):
        if not 0 <= value <= _MAX_UNIX_TIMESTAMP:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"{field_name} is outside the supported Unix timestamp range",
            )
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if not isinstance(value, str):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"{field_name} must be Unix seconds or an ISO-8601 timestamp",
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
