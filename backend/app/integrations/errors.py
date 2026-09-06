"""Stable validation failures shared by integration contracts."""

from __future__ import annotations

from enum import Enum


class ValidationCode(str, Enum):
    MALFORMED_ENVELOPE = "malformed_envelope"
    INVALID_SIGNATURE = "invalid_signature"
    UNAUTHORIZED_REPOSITORY = "unauthorized_repository"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    PROHIBITED_CAPABILITY = "prohibited_capability"
    INVALID_BUDGET = "invalid_budget"
    TASK_IDENTITY_MISMATCH = "task_identity_mismatch"
    ISSUE_REVISION_MISMATCH = "issue_revision_mismatch"
    OUTPUT_SCHEMA_MISMATCH = "output_schema_mismatch"
    OUTPUT_PAYLOAD_MISMATCH = "output_payload_mismatch"
    STALE_RESULT = "stale_result"
    LATE_RESULT = "late_result"
    PROHIBITED_COMMAND = "prohibited_command"


class ContractValidationError(ValueError):
    def __init__(self, code: ValidationCode, message: str) -> None:
        super().__init__(message)
        self.code = code
