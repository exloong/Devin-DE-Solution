"""Devin Review boundary: trigger, status, findings, and advisory limits."""

from __future__ import annotations

import pytest
from conftest import TARGET_SHA

from app.integrations import (
    ContractValidationError,
    FakeDevinReviewAdapter,
    FindingSeverity,
    HttpResponse,
    LiveDevinReviewClient,
    RecordedTransport,
    ReviewFinding,
    ReviewRequest,
    ReviewStatus,
    StaticTokenProvider,
    TargetCommit,
    ValidationCode,
    map_review_status,
)

TOKEN = StaticTokenProvider("test-token")


def request(number: int = 101) -> ReviewRequest:
    return ReviewRequest(
        pull_request_number=number, head_commit=TargetCommit(sha=TARGET_SHA)
    )


def test_review_requests_only_target_superset() -> None:
    with pytest.raises(ContractValidationError) as error:
        ReviewRequest(
            pull_request_number=101,
            head_commit=TargetCommit(sha=TARGET_SHA),
            repository="apache/superset",  # type: ignore[arg-type]
        )
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_review_request_requires_a_positive_pull_request_number() -> None:
    with pytest.raises(ContractValidationError):
        ReviewRequest(pull_request_number=0, head_commit=TargetCommit(sha=TARGET_SHA))


def test_review_request_exposes_the_superset_pull_request_url() -> None:
    assert request().pull_request_url == (
        "https://github.com/exloong/superset/pull/101"
    )


def test_fake_review_runs_deterministically_to_completion() -> None:
    adapter = FakeDevinReviewAdapter()

    triggered = adapter.trigger_review(request())
    assert triggered.status is ReviewStatus.QUEUED
    assert triggered.findings == ()

    assert adapter.advance(triggered.review_id).status is ReviewStatus.RUNNING
    completed = adapter.advance(triggered.review_id)

    assert completed.status is ReviewStatus.COMPLETED
    assert completed.findings
    assert completed.review_url.startswith("https://app.devin.ai/review/")


def test_fake_review_ids_are_stable_for_the_same_head_commit() -> None:
    first = FakeDevinReviewAdapter().trigger_review(request())
    second = FakeDevinReviewAdapter().trigger_review(request())

    assert first.review_id == second.review_id


def test_findings_are_unavailable_until_the_run_is_terminal() -> None:
    adapter = FakeDevinReviewAdapter()
    triggered = adapter.trigger_review(request())

    with pytest.raises(ContractValidationError):
        adapter.list_findings(triggered.review_id)

    adapter.run_to_completion(triggered.review_id)
    assert adapter.list_findings(triggered.review_id)


def test_review_never_approves_a_merge() -> None:
    adapter = FakeDevinReviewAdapter()
    triggered = adapter.trigger_review(request())
    completed = adapter.run_to_completion(triggered.review_id)

    assert completed.approves_merge is False


def test_highest_severity_is_reported_for_operator_triage() -> None:
    adapter = FakeDevinReviewAdapter(
        findings=(
            ReviewFinding(
                finding_id="1",
                severity=FindingSeverity.LOW,
                title="nit",
                path=None,
                line=None,
                summary="advisory",
            ),
            ReviewFinding(
                finding_id="2",
                severity=FindingSeverity.HIGH,
                title="missing regression test",
                path="tests/unit_tests/relay_regression_test.py",
                line=10,
                summary="advisory",
            ),
        )
    )
    triggered = adapter.trigger_review(request())
    completed = adapter.run_to_completion(triggered.review_id)

    assert completed.highest_severity is FindingSeverity.HIGH


def test_unknown_review_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as error:
        FakeDevinReviewAdapter().get_review("review-missing")
    assert error.value.code is ValidationCode.UNKNOWN_SESSION


def test_unsupported_review_status_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as error:
        map_review_status("mystery")
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_live_client_triggers_a_review_through_the_injected_transport() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {
                    "review_id": "review-1",
                    "status": "queued",
                    "created_at": "2026-01-01T12:00:00Z",
                },
            )
        ]
    )
    client = LiveDevinReviewClient(transport=transport, token_provider=TOKEN)

    run = client.trigger_review(request())

    assert run.status is ReviewStatus.QUEUED
    assert transport.requests[0].url == "https://api.devin.ai/v1/reviews"
    assert transport.requests[0].json_body == {
        "repository": "exloong/superset",
        "pull_request_number": 101,
        "head_commit": TARGET_SHA,
    }
    assert "test-token" not in str(transport.redacted_requests[0])


def test_live_client_parses_completed_findings_and_redacts_them() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {
                    "review_id": "review-1",
                    "status": "completed",
                    "repository": "exloong/superset",
                    "pull_request_number": 101,
                    "head_commit": TARGET_SHA,
                    "created_at": "2026-01-01T12:00:00Z",
                    "updated_at": "2026-01-01T12:10:00Z",
                    "findings": [
                        {
                            "id": "f-1",
                            "severity": "medium",
                            "title": "Guard the null branch",
                            "path": "superset/charts/api.py",
                            "line": 88,
                            "summary": "seen with token: ghp_0123456789abcdefghij",
                        }
                    ],
                },
            )
        ]
    )
    client = LiveDevinReviewClient(transport=transport, token_provider=TOKEN)

    run = client.get_review("review-1")

    assert run.status is ReviewStatus.COMPLETED
    assert run.findings[0].severity is FindingSeverity.MEDIUM
    assert "ghp_0123456789abcdefghij" not in run.findings[0].summary


def test_live_client_rejects_a_review_for_another_repository() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {
                    "review_id": "review-1",
                    "status": "queued",
                    "repository": "apache/superset",
                    "pull_request_number": 101,
                    "head_commit": TARGET_SHA,
                    "created_at": "2026-01-01T12:00:00Z",
                },
            )
        ]
    )
    client = LiveDevinReviewClient(transport=transport, token_provider=TOKEN)

    with pytest.raises(ContractValidationError) as error:
        client.get_review("review-1")
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_live_client_rejects_a_malformed_finding() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                {"findings": [{"id": "f-1", "severity": "medium"}]},
            )
        ]
    )
    client = LiveDevinReviewClient(transport=transport, token_provider=TOKEN)

    with pytest.raises(ContractValidationError) as error:
        client.list_findings("review-1")
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_live_client_rejects_a_transport_failure() -> None:
    transport = RecordedTransport([HttpResponse(503, {})])
    client = LiveDevinReviewClient(transport=transport, token_provider=TOKEN)

    with pytest.raises(ContractValidationError) as error:
        client.trigger_review(request())
    assert error.value.code is ValidationCode.TRANSPORT_FAILURE


def test_live_client_rejects_a_malformed_review_id() -> None:
    client = LiveDevinReviewClient(transport=RecordedTransport(), token_provider=TOKEN)

    with pytest.raises(ContractValidationError):
        client.get_review("review 1/../admin")
