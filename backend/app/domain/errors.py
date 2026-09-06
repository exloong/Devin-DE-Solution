from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    ILLEGAL_TRANSITION = "illegal_transition"
    VERSION_CONFLICT = "version_conflict"
    STALE_RESULT = "stale_result"
    UNAUTHORIZED_ACTOR = "unauthorized_actor"
    HUMAN_GATE_REQUIRED = "human_gate_required"
    MISSING_EVIDENCE = "missing_evidence"
    SECURITY_FAIL_CLOSED = "security_fail_closed"
    REPOSITORY_NOT_ALLOWED = "repository_not_allowed"
    PROHIBITED_ACTION = "prohibited_action"
    ISSUE_LOCKED = "issue_locked"
    INTERNAL = "internal_error"


HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.INVALID_INPUT: 422,
    ErrorCode.ILLEGAL_TRANSITION: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.STALE_RESULT: 409,
    ErrorCode.UNAUTHORIZED_ACTOR: 403,
    ErrorCode.HUMAN_GATE_REQUIRED: 403,
    ErrorCode.MISSING_EVIDENCE: 409,
    ErrorCode.SECURITY_FAIL_CLOSED: 409,
    ErrorCode.REPOSITORY_NOT_ALLOWED: 403,
    ErrorCode.PROHIBITED_ACTION: 403,
    ErrorCode.ISSUE_LOCKED: 423,
    ErrorCode.INTERNAL: 500,
}


class DomainError(Exception):
    """Stable, code-carrying error surfaced to the API without private payloads."""

    def __init__(self, code: ErrorCode, message: str, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, object] = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]
