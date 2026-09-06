"""Stable validation failures shared by Relay integration boundaries."""

from __future__ import annotations

from enum import Enum


class ValidationCode(str, Enum):
    """Stable, machine-readable code for a rejected integration input."""

    MALFORMED_ENVELOPE = "malformed_envelope"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_SIGNATURE = "invalid_signature"
    UNAUTHORIZED_REPOSITORY = "unauthorized_repository"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    PROHIBITED_CAPABILITY = "prohibited_capability"
    PROHIBITED_COMMAND = "prohibited_command"
    INVALID_BUDGET = "invalid_budget"
    TASK_IDENTITY_MISMATCH = "task_identity_mismatch"
    ISSUE_REVISION_MISMATCH = "issue_revision_mismatch"
    TARGET_COMMIT_MISMATCH = "target_commit_mismatch"
    OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"
    OUTPUT_PAYLOAD_MISMATCH = "output_payload_mismatch"
    STALE_RESULT = "stale_result"
    LATE_RESULT = "late_result"
    UNKNOWN_SESSION = "unknown_session"
    TRANSPORT_FAILURE = "transport_failure"
    AMBIGUOUS_OWNERSHIP = "ambiguous_ownership"


class ContractValidationError(ValueError):
    """Raised when an integration input violates a Relay contract."""

    def __init__(self, code: ValidationCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:
        return f"[{self.code.value}] {super().__str__()}"
