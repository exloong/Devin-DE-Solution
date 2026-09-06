"""Devin Review boundary: trigger, status, and findings for Superset PRs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from .errors import ContractValidationError, ValidationCode
from .redaction import redact_text
from .repository import (
    SUPERSET_FULL_NAME,
    RepositoryIdentity,
    TargetCommit,
    require_superset_repository,
)
from .transport import (
    HttpRequest,
    HttpTransport,
    parse_timestamp,
    require_field,
    require_json_object,
)

DEVIN_REVIEW_API_ROOT = "https://api.devin.ai/v1"
FAKE_REVIEW_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ReviewStatus(str, Enum):
    """Lifecycle of one Devin Review run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {ReviewStatus.COMPLETED, ReviewStatus.FAILED}


_REVIEW_STATUS_MAP: Mapping[str, ReviewStatus] = {
    "queued": ReviewStatus.QUEUED,
    "pending": ReviewStatus.QUEUED,
    "running": ReviewStatus.RUNNING,
    "in_progress": ReviewStatus.RUNNING,
    "completed": ReviewStatus.COMPLETED,
    "finished": ReviewStatus.COMPLETED,
    "failed": ReviewStatus.FAILED,
    "error": ReviewStatus.FAILED,
}


def map_review_status(value: object) -> ReviewStatus:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, "review status is missing"
        )
    status = _REVIEW_STATUS_MAP.get(value.strip().lower())
    if status is None:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            f"review status {value!r} is unsupported",
        )
    return status


class FindingSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def parse(cls, value: object) -> FindingSeverity:
        if not isinstance(value, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "finding severity is missing"
            )
        try:
            return cls(value.strip().lower())
        except ValueError as error:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"finding severity {value!r} is unsupported",
            ) from error


@dataclass(frozen=True)
class ReviewRequest:
    """A request to review one Superset pull-request head commit."""

    pull_request_number: int
    head_commit: TargetCommit
    repository: RepositoryIdentity = field(
        default_factory=lambda: require_superset_repository(SUPERSET_FULL_NAME)
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "repository", require_superset_repository(self.repository)
        )
        if not isinstance(self.pull_request_number, int) or (
            isinstance(self.pull_request_number, bool) or self.pull_request_number < 1
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "pull_request_number must be a positive integer",
            )
        if not isinstance(self.head_commit, TargetCommit):
            raise ContractValidationError(
                ValidationCode.MALFORMED_ENVELOPE,
                "head_commit must be a TargetCommit",
            )

    @property
    def pull_request_url(self) -> str:
        return (
            f"https://github.com/{self.repository.full_name}/pull/"
            f"{self.pull_request_number}"
        )


@dataclass(frozen=True)
class ReviewFinding:
    """One Devin Review finding, carried as advisory evidence only."""

    finding_id: str
    severity: FindingSeverity
    title: str
    path: str | None
    line: int | None
    summary: str


@dataclass(frozen=True)
class ReviewRun:
    """Status of one review run and its findings."""

    review_id: str
    request: ReviewRequest
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime
    review_url: str
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.review_url, str) or not self.review_url.startswith(
            "https://"
        ):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "review URL must be an https link"
            )
        if self.findings and not self.status.is_terminal:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "a non-terminal review may not report findings",
            )

    @property
    def approves_merge(self) -> bool:
        """Devin Review never approves a merge; humans do."""
        return False

    @property
    def highest_severity(self) -> FindingSeverity | None:
        if not self.findings:
            return None
        order = {
            FindingSeverity.LOW: 0,
            FindingSeverity.MEDIUM: 1,
            FindingSeverity.HIGH: 2,
        }
        return max(
            (finding.severity for finding in self.findings),
            key=lambda severity: order[severity],
        )


class DevinReviewClient(Protocol):
    """Devin Review operations Relay depends on."""

    def trigger_review(self, request: ReviewRequest) -> ReviewRun:
        """Trigger a review for one Superset pull-request head commit."""

    def get_review(self, review_id: str) -> ReviewRun:
        """Return the current status of a review run."""

    def list_findings(self, review_id: str) -> tuple[ReviewFinding, ...]:
        """Return the findings of a completed review run."""


def _review_url(review_id: str, request: ReviewRequest) -> str:
    return (
        f"https://app.devin.ai/review/{request.repository.owner}/"
        f"{request.repository.name}/pull/{request.pull_request_number}"
        f"?run={review_id}"
    )


class FakeDevinReviewAdapter:
    """Deterministic review adapter used by tests and local dry runs."""

    def __init__(
        self,
        *,
        findings: tuple[ReviewFinding, ...] | None = None,
        started_at: datetime = FAKE_REVIEW_EPOCH,
    ) -> None:
        self._findings_template = findings
        self._started_at = started_at
        self._runs: dict[str, ReviewRun] = {}
        self._lock = Lock()

    def trigger_review(self, request: ReviewRequest) -> ReviewRun:
        review_id = (
            "review-"
            + uuid5(
                NAMESPACE_URL,
                f"{request.repository.full_name}/{request.pull_request_number}/"
                f"{request.head_commit.sha}",
            ).hex[:16]
        )
        created_at = self._started_at
        run = ReviewRun(
            review_id=review_id,
            request=request,
            status=ReviewStatus.QUEUED,
            created_at=created_at,
            updated_at=created_at,
            review_url=_review_url(review_id, request),
        )
        with self._lock:
            self._runs[review_id] = run
        return run

    def advance(self, review_id: str) -> ReviewRun:
        """Move a review run one deterministic step forward."""
        with self._lock:
            run = self._require(review_id)
            if run.status is ReviewStatus.QUEUED:
                next_status = ReviewStatus.RUNNING
                findings: tuple[ReviewFinding, ...] = ()
            elif run.status is ReviewStatus.RUNNING:
                next_status = ReviewStatus.COMPLETED
                findings = (
                    self._findings_template
                    if self._findings_template is not None
                    else self._default_findings(run.request)
                )
            else:
                return run
            updated = ReviewRun(
                review_id=run.review_id,
                request=run.request,
                status=next_status,
                created_at=run.created_at,
                updated_at=run.updated_at + timedelta(seconds=45),
                review_url=run.review_url,
                findings=findings,
            )
            self._runs[review_id] = updated
        return updated

    def run_to_completion(self, review_id: str) -> ReviewRun:
        run = self.advance(review_id)
        while not run.status.is_terminal:
            run = self.advance(review_id)
        return run

    def get_review(self, review_id: str) -> ReviewRun:
        with self._lock:
            return self._require(review_id)

    def list_findings(self, review_id: str) -> tuple[ReviewFinding, ...]:
        run = self.get_review(review_id)
        if not run.status.is_terminal:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"review {review_id} is {run.status.value}, not complete",
            )
        return run.findings

    def _require(self, review_id: str) -> ReviewRun:
        run = self._runs.get(review_id)
        if run is None:
            raise ContractValidationError(
                ValidationCode.UNKNOWN_SESSION, f"review {review_id} is unknown"
            )
        return run

    @staticmethod
    def _default_findings(request: ReviewRequest) -> tuple[ReviewFinding, ...]:
        return (
            ReviewFinding(
                finding_id=f"{request.head_commit.short_sha}-1",
                severity=FindingSeverity.LOW,
                title="Add a regression test assertion for the fixed branch",
                path="tests/unit_tests/relay_regression_test.py",
                line=42,
                summary=(
                    "The deterministic fake review reports one advisory finding "
                    "so operators can exercise the owner packet."
                ),
            ),
        )


@dataclass
class LiveDevinReviewClient:
    """Devin Review client built on an injected transport."""

    transport: HttpTransport
    token_provider: Any
    api_root: str = DEVIN_REVIEW_API_ROOT
    correlation_id: str | None = None

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.transport.send(
            HttpRequest(
                method=method,
                url=f"{self.api_root}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token_provider.token()}",
                },
                json_body=json_body,
                correlation_id=self.correlation_id,
            )
        )

    def trigger_review(self, request: ReviewRequest) -> ReviewRun:
        require_superset_repository(request.repository)
        payload = require_json_object(
            self._send(
                "POST",
                "/reviews",
                json_body={
                    "repository": request.repository.full_name,
                    "pull_request_number": request.pull_request_number,
                    "head_commit": request.head_commit.sha,
                },
            ),
            action="trigger review",
        )
        return self._run_from_payload(payload, request=request)

    def get_review(self, review_id: str) -> ReviewRun:
        payload = require_json_object(
            self._send("GET", f"/reviews/{_normalize_review_id(review_id)}"),
            action="get review",
        )
        return self._run_from_payload(payload)

    def list_findings(self, review_id: str) -> tuple[ReviewFinding, ...]:
        payload = require_json_object(
            self._send("GET", f"/reviews/{_normalize_review_id(review_id)}/findings"),
            action="list review findings",
        )
        return _findings_from_payload(payload.get("findings"))

    def _run_from_payload(
        self, payload: Mapping[str, Any], *, request: ReviewRequest | None = None
    ) -> ReviewRun:
        action = "review response"
        review_id = _normalize_review_id(
            require_field(payload, "review_id", str, action=action)
        )
        status = map_review_status(payload.get("status"))
        resolved_request = request or ReviewRequest(
            pull_request_number=require_field(
                payload, "pull_request_number", int, action=action
            ),
            head_commit=TargetCommit(
                sha=require_field(payload, "head_commit", str, action=action)
            ),
            repository=require_superset_repository(
                str(payload.get("repository", SUPERSET_FULL_NAME))
            ),
        )
        created_at = parse_timestamp(payload.get("created_at"), "created_at")
        updated_at = parse_timestamp(
            payload.get("updated_at"), "updated_at", default=created_at
        )
        return ReviewRun(
            review_id=review_id,
            request=resolved_request,
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            review_url=str(
                payload.get("review_url") or _review_url(review_id, resolved_request)
            ),
            findings=(
                _findings_from_payload(payload.get("findings"))
                if status.is_terminal
                else ()
            ),
        )


def _normalize_review_id(review_id: object) -> str:
    if not isinstance(review_id, str) or not review_id.strip():
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, "review id must be non-empty text"
        )
    normalized = review_id.strip()
    if any(character in normalized for character in "/?#& "):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, "review id is malformed"
        )
    return normalized


def _findings_from_payload(value: object) -> tuple[ReviewFinding, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE, "review findings must be a list"
        )
    findings: list[ReviewFinding] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "review finding is malformed"
            )
        line = entry.get("line")
        if line is not None and (isinstance(line, bool) or not isinstance(line, int)):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "review finding line is malformed"
            )
        path = entry.get("path")
        if path is not None and not isinstance(path, str):
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE, "review finding path is malformed"
            )
        findings.append(
            ReviewFinding(
                finding_id=require_field(entry, "id", str, action="review finding"),
                severity=FindingSeverity.parse(entry.get("severity")),
                title=require_field(entry, "title", str, action="review finding"),
                path=path,
                line=line,
                summary=redact_text(
                    require_field(entry, "summary", str, action="review finding")
                ),
            )
        )
    return tuple(findings)
