"""Devin Review boundary: trigger, status, and findings for Superset PRs.

The live client follows the documented enterprise PR-review contract, which
identifies a review by its pull-request URL and head commit rather than by a
review identifier, so Relay never invents an id, endpoint, or review URL.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Protocol

from .errors import ContractValidationError, ValidationCode
from .json_values import JsonObject
from .repository import (
    SUPERSET_FULL_NAME,
    SUPERSET_REPOSITORY,
    RepositoryIdentity,
    TargetCommit,
    superset_pull_request_url,
    validate_pull_request_url,
    validate_superset_repository_field,
)
from .transport import (
    HttpRequest,
    HttpResponse,
    HttpTransport,
    TokenProvider,
    optional_str,
    parse_timestamp,
    require_int,
    require_json_object,
    require_str,
)

DEVIN_REVIEW_API_ROOT = "https://api.devin.ai/v3"
PR_REVIEWS_PATH = "/enterprise/pr-reviews"
FAKE_REVIEW_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ReviewStatus(str, Enum):
    """Lifecycle of one Devin Review run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ReviewStatus.COMPLETED,
            ReviewStatus.FAILED,
            ReviewStatus.CANCELLED,
        }


_REVIEW_STATUS_MAP: Mapping[str, ReviewStatus] = {
    "pending": ReviewStatus.QUEUED,
    "running": ReviewStatus.RUNNING,
    "completed": ReviewStatus.COMPLETED,
    "errored": ReviewStatus.FAILED,
    "cancelled": ReviewStatus.CANCELLED,
}


def map_review_status(value: object) -> ReviewStatus:
    """Map a documented review status, failing closed on anything else."""
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
    repository: RepositoryIdentity = SUPERSET_REPOSITORY

    def __post_init__(self) -> None:
        validate_superset_repository_field(self.repository)
        if isinstance(self.pull_request_number, bool) or (
            not isinstance(self.pull_request_number, int)
            or self.pull_request_number < 1
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
        return superset_pull_request_url(self.pull_request_number)


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
    """Status of the review of one pull-request head commit.

    The documented contract has no review identifier, so a run is identified
    by its pull-request URL and immutable head commit.
    """

    request: ReviewRequest
    status: ReviewStatus
    created_at: datetime
    updated_at: datetime
    findings: tuple[ReviewFinding, ...] = ()

    def __post_init__(self) -> None:
        validate_superset_repository_field(self.request.repository)
        if self.findings and not self.status.is_terminal:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                "a non-terminal review may not report findings",
            )

    @property
    def pull_request_url(self) -> str:
        return self.request.pull_request_url

    @property
    def commit_sha(self) -> str:
        return self.request.head_commit.sha

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

    def get_review(self, request: ReviewRequest) -> ReviewRun:
        """Return the latest review of that pull request and commit."""

    def latest_review(self, request: ReviewRequest) -> ReviewRun | None:
        """Return the latest review, or ``None`` when none exists yet."""

    def list_findings(self, request: ReviewRequest) -> tuple[ReviewFinding, ...]:
        """Return the findings of a completed review run, when available."""


def _review_key(request: ReviewRequest) -> tuple[str, str]:
    return (request.pull_request_url, request.head_commit.sha)


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
        self._runs: dict[tuple[str, str], ReviewRun] = {}
        self._lock = Lock()

    def trigger_review(self, request: ReviewRequest) -> ReviewRun:
        run = ReviewRun(
            request=request,
            status=ReviewStatus.QUEUED,
            created_at=self._started_at,
            updated_at=self._started_at,
        )
        with self._lock:
            self._runs[_review_key(request)] = run
        return run

    def advance(self, request: ReviewRequest) -> ReviewRun:
        """Move a review run one deterministic step forward."""
        with self._lock:
            run = self._require(request)
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
                request=run.request,
                status=next_status,
                created_at=run.created_at,
                updated_at=run.updated_at + timedelta(seconds=45),
                findings=findings,
            )
            self._runs[_review_key(run.request)] = updated
        return updated

    def run_to_completion(self, request: ReviewRequest) -> ReviewRun:
        run = self.advance(request)
        while not run.status.is_terminal:
            run = self.advance(request)
        return run

    def get_review(self, request: ReviewRequest) -> ReviewRun:
        with self._lock:
            return self._require(request)

    def latest_review(self, request: ReviewRequest) -> ReviewRun | None:
        with self._lock:
            return self._runs.get(_review_key(request))

    def list_findings(self, request: ReviewRequest) -> tuple[ReviewFinding, ...]:
        run = self.get_review(request)
        if not run.status.is_terminal:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"review of {run.pull_request_url} is {run.status.value}, "
                "not complete",
            )
        return run.findings

    def _require(self, request: ReviewRequest) -> ReviewRun:
        run = self._runs.get(_review_key(request))
        if run is None:
            raise ContractValidationError(
                ValidationCode.UNKNOWN_SESSION,
                f"no review exists for {request.pull_request_url} at "
                f"{request.head_commit.short_sha}",
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
    token_provider: TokenProvider
    api_root: str = DEVIN_REVIEW_API_ROOT
    correlation_id: str | None = None

    def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: JsonObject | None = None,
        query: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self.transport.send(
            HttpRequest(
                method=method,
                url=f"{self.api_root}{path}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.token_provider.token()}",
                },
                json_body=json_body,
                query=dict(query or {}),
                correlation_id=self.correlation_id,
            )
        )

    def trigger_review(self, request: ReviewRequest) -> ReviewRun:
        validate_superset_repository_field(request.repository)
        payload = require_json_object(
            self._send(
                "POST",
                PR_REVIEWS_PATH,
                json_body={"pr_url": request.pull_request_url},
            ),
            action="trigger review",
        )
        return _run_from_payload(payload, request=request, action="trigger review")

    def get_review(self, request: ReviewRequest) -> ReviewRun:
        run = self.latest_review(request)
        if run is None:
            raise ContractValidationError(
                ValidationCode.UNKNOWN_SESSION,
                f"no review exists for {request.pull_request_url} at "
                f"{request.head_commit.short_sha}",
            )
        return run

    def latest_review(self, request: ReviewRequest) -> ReviewRun | None:
        """Look up the latest review of the request's immutable head commit."""
        validate_superset_repository_field(request.repository)
        action = "get latest review"
        response = self._send(
            "GET",
            PR_REVIEWS_PATH,
            query={
                "pr_url": request.pull_request_url,
                "commit_sha": request.head_commit.sha,
            },
        )
        if response.status_code == 404:
            return None
        payload = require_json_object(response, action=action)
        return _run_from_payload(payload, request=request, action=action)

    def list_findings(self, request: ReviewRequest) -> tuple[ReviewFinding, ...]:
        """Return no findings: the documented API exposes no findings route.

        The enterprise contract only triggers a review and reports its latest
        status, so Relay treats live findings as unavailable rather than
        calling an invented endpoint. Findings reach operators through the
        review's own GitHub comments, which a human reads on the pull request.
        """
        run = self.get_review(request)
        if not run.status.is_terminal:
            raise ContractValidationError(
                ValidationCode.MALFORMED_RESPONSE,
                f"review of {run.pull_request_url} is {run.status.value}, "
                "not complete",
            )
        return ()


def _run_from_payload(
    payload: JsonObject, *, request: ReviewRequest, action: str
) -> ReviewRun:
    """Build a run from a documented review payload, validating its identity."""
    repo_path = require_str(payload, "repo_path", action=action)
    if repo_path.strip().lower() != SUPERSET_FULL_NAME:
        raise ContractValidationError(
            ValidationCode.UNAUTHORIZED_REPOSITORY,
            f"review response targets {repo_path} rather than {SUPERSET_FULL_NAME}",
        )
    pr_number = require_int(payload, "pr_number", action=action)
    if pr_number != request.pull_request_number:
        raise ContractValidationError(
            ValidationCode.MALFORMED_RESPONSE,
            "review response names a different pull request",
        )
    commit = TargetCommit(sha=require_str(payload, "commit_sha", action=action))
    if commit != request.head_commit:
        raise ContractValidationError(
            ValidationCode.TARGET_COMMIT_MISMATCH,
            "review response names a different head commit",
        )
    pr_url = optional_str(payload, "pr_url", action=action)
    if pr_url is not None:
        validate_pull_request_url(pr_url, pull_request_number=pr_number)
    status = map_review_status(payload.get("status"))
    created_at = parse_timestamp(payload.get("created_at"), "created_at")
    updated_at = parse_timestamp(
        payload.get("updated_at"), "updated_at", default=created_at
    )
    # The documented response carries no findings, so none are parsed from it.
    return ReviewRun(
        request=request,
        status=status,
        created_at=created_at,
        updated_at=updated_at,
    )
