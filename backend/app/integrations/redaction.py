"""Redaction helpers applied before integration payloads are retained."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "[redacted]"

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
    value: Any,
    *,
    redacted_keys: frozenset[str] = DEFAULT_REDACTED_KEYS,
    max_depth: int = 12,
) -> Any:
    """Return a copy of ``value`` with configured keys and secrets removed."""
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
    if isinstance(value, (bytes, bytearray)):
        return REDACTED
    if isinstance(value, Sequence):
        return [
            redact_value(item, redacted_keys=redacted_keys, max_depth=max_depth - 1)
            for item in value
        ]
    return value


def redact_mapping(
    payload: Mapping[str, Any],
    *,
    redacted_keys: frozenset[str] = DEFAULT_REDACTED_KEYS,
) -> dict[str, Any]:
    """Redact a mapping, preserving structure for audit records."""
    redacted = redact_value(payload, redacted_keys=redacted_keys)
    if not isinstance(redacted, dict):  # pragma: no cover - defensive
        raise TypeError("redacted payload must remain a mapping")
    return redacted
