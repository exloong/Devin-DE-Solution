"""Redaction helpers applied before integration payloads are retained."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .json_values import JsonObject, JsonValue

REDACTED = "[redacted]"
_MAX_REDACTION_DEPTH = 12

DEFAULT_REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "credentials",
        "id_token",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "session_token",
        "set-cookie",
        "token",
        "webhook_secret",
        "x-hub-signature-256",
    }
)

_QUERY_SECRET_PATTERN = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|signature|sig)=)[^&#\s]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_GITHUB_TOKEN_PATTERN = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{16,})\b")


def redact_text(value: str) -> str:
    """Remove credential-shaped substrings from free text and URLs."""
    redacted = _QUERY_SECRET_PATTERN.sub(rf"\1{REDACTED}", value)
    redacted = _BEARER_PATTERN.sub(rf"\1 {REDACTED}", redacted)
    return _GITHUB_TOKEN_PATTERN.sub(REDACTED, redacted)


def _is_redacted_key(key: str, redacted_keys: frozenset[str]) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in {name.replace("-", "_") for name in redacted_keys}


def redact_value(
    value: object,
    *,
    redacted_keys: frozenset[str] = DEFAULT_REDACTED_KEYS,
    max_depth: int = _MAX_REDACTION_DEPTH,
) -> JsonValue:
    """Return a JSON copy of ``value`` with configured keys and secrets gone.

    Anything that is not JSON-shaped is replaced by the redaction marker, so an
    unexpected object can never leak into a retained audit record.
    """
    if max_depth <= 0:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            str(key): (
                REDACTED
                if _is_redacted_key(str(key), redacted_keys)
                else redact_value(
                    item, redacted_keys=redacted_keys, max_depth=max_depth - 1
                )
            )
            for key, item in value.items()
        }
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return REDACTED
    if isinstance(value, Sequence):
        return [
            redact_value(item, redacted_keys=redacted_keys, max_depth=max_depth - 1)
            for item in value
        ]
    return REDACTED


def redact_mapping(
    payload: JsonObject,
    *,
    redacted_keys: frozenset[str] = DEFAULT_REDACTED_KEYS,
) -> dict[str, JsonValue]:
    """Redact a mapping, preserving structure for audit records."""
    redacted = redact_value(payload, redacted_keys=redacted_keys)
    if not isinstance(redacted, dict):  # pragma: no cover - defensive
        raise TypeError("redacted payload must remain a mapping")
    return redacted
