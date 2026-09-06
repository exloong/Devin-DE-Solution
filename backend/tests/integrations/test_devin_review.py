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
from app.integrations.json_values import JsonValue

TOKEN = StaticTokenProvider("test-token")
PR_REVIEWS_URL = "https://api.devin.ai/v3/enterprise/pr-reviews"
PR_URL = "https://github.com/exloong/superset/pull/101"
CREATED_AT = 1_767_268_800


def request(number: int = 101) -> ReviewRequest:
    return ReviewRequest(
        pull_request_number=number, head_commit=TargetCommit(sha=TARGET_SHA)
    )


def review_payload(**overrides: JsonValue) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "pr_url": PR_URL,
        "repo_path": "exloong/superset",
        "pr_number": 101,
        "commit_sha": TARGET_SHA,
        "status": "pending",
        "created_at": CREATED_AT,
    }
    payload.update(overrides)
    return payload


def live_client(transport: RecordedTransport) -> LiveDevinReviewClient:
    return LiveDevinReviewClient(transport=transport, token_provider=TOKEN)


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

    assert adapter.advance(request()).status is ReviewStatus.RUNNING
    completed = adapter.advance(request())

    assert completed.status is ReviewStatus.COMPLETED
    assert completed.findings
    assert completed.pull_request_url == PR_URL
    assert completed.commit_sha == TARGET_SHA


def test_a_review_is_identified_by_its_pull_request_and_commit() -> None:
    adapter = FakeDevinReviewAdapter()
    adapter.trigger_review(request())

    other_commit = ReviewRequest(
        pull_request_number=101, head_commit=TargetCommit(sha="b" * 40)
    )

    assert adapter.latest_review(request()) is not None
    assert adapter.latest_review(other_commit) is None


def test_findings_are_unavailable_until_the_run_is_terminal() -> None:
    adapter = FakeDevinReviewAdapter()
    adapter.trigger_review(request())

    with pytest.raises(ContractValidationError):
        adapter.list_findings(request())

    adapter.run_to_completion(request())
    assert adapter.list_findings(request())


def test_review_never_approves_a_merge() -> None:
    adapter = FakeDevinReviewAdapter()
    adapter.trigger_review(request())
    completed = adapter.run_to_completion(request())

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
    adapter.trigger_review(request())
    completed = adapter.run_to_completion(request())

    assert completed.highest_severity is FindingSeverity.HIGH


def test_unknown_review_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as error:
        FakeDevinReviewAdapter().get_review(request())
    assert error.value.code is ValidationCode.UNKNOWN_SESSION


def test_documented_review_statuses_map_onto_relay_states() -> None:
    assert map_review_status("pending") is ReviewStatus.QUEUED
    assert map_review_status("running") is ReviewStatus.RUNNING
    assert map_review_status("completed") is ReviewStatus.COMPLETED
    assert map_review_status("errored") is ReviewStatus.FAILED
    assert map_review_status("cancelled") is ReviewStatus.CANCELLED


def test_unsupported_review_status_is_rejected() -> None:
    with pytest.raises(ContractValidationError) as error:
        map_review_status("mystery")
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_live_client_triggers_a_review_with_only_the_pull_request_url() -> None:
    transport = RecordedTransport([HttpResponse(200, review_payload())])

    run = live_client(transport).trigger_review(request())

    assert run.status is ReviewStatus.QUEUED
    assert run.created_at.isoformat() == "2026-01-01T12:00:00+00:00"
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].url == PR_REVIEWS_URL
    assert transport.requests[0].json_body == {"pr_url": PR_URL}
    assert "test-token" not in str(transport.redacted_requests[0])


def test_live_client_looks_up_the_latest_review_for_the_head_commit() -> None:
    transport = RecordedTransport([HttpResponse(200, review_payload(status="running"))])

    run = live_client(transport).get_review(request())

    assert run.status is ReviewStatus.RUNNING
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url == PR_REVIEWS_URL
    assert transport.requests[0].query == {
        "pr_url": PR_URL,
        "commit_sha": TARGET_SHA,
    }


def test_a_missing_review_is_reported_rather_than_guessed() -> None:
    transport = RecordedTransport([HttpResponse(404, {}), HttpResponse(404, {})])
    client = live_client(transport)

    assert client.latest_review(request()) is None
    with pytest.raises(ContractValidationError) as error:
        client.get_review(request())
    assert error.value.code is ValidationCode.UNKNOWN_SESSION


def test_live_findings_are_unavailable_because_no_route_is_documented() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                review_payload(status="completed", updated_at=CREATED_AT + 600),
            )
        ]
    )

    assert live_client(transport).list_findings(request()) == ()
    assert [request_.url for request_ in transport.requests] == [PR_REVIEWS_URL]


def test_live_client_rejects_a_review_for_another_repository() -> None:
    transport = RecordedTransport(
        [HttpResponse(200, review_payload(repo_path="apache/superset"))]
    )

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).get_review(request())
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_live_client_rejects_a_review_of_another_pull_request() -> None:
    transport = RecordedTransport([HttpResponse(200, review_payload(pr_number=102))])

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).get_review(request())
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_live_client_rejects_a_review_of_another_commit() -> None:
    transport = RecordedTransport(
        [HttpResponse(200, review_payload(commit_sha="b" * 40))]
    )

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).get_review(request())
    assert error.value.code is ValidationCode.TARGET_COMMIT_MISMATCH


def test_live_client_rejects_a_pull_request_url_outside_superset() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                review_payload(pr_url="https://github.com/apache/superset/pull/101"),
            )
        ]
    )

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).get_review(request())
    assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_live_client_ignores_undocumented_findings_in_a_response() -> None:
    transport = RecordedTransport(
        [
            HttpResponse(
                200,
                review_payload(
                    status="completed",
                    findings=[{"id": "f-1", "severity": "medium"}],
                ),
            )
        ]
    )

    assert live_client(transport).get_review(request()).findings == ()


def test_live_client_rejects_an_unsupported_status() -> None:
    transport = RecordedTransport([HttpResponse(200, review_payload(status="queued"))])

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).trigger_review(request())
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE


def test_live_client_rejects_a_transport_failure() -> None:
    transport = RecordedTransport([HttpResponse(503, {})])

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).trigger_review(request())
    assert error.value.code is ValidationCode.TRANSPORT_FAILURE


def test_findings_are_not_exposed_for_a_running_review() -> None:
    transport = RecordedTransport([HttpResponse(200, review_payload(status="running"))])

    with pytest.raises(ContractValidationError) as error:
        live_client(transport).list_findings(request())
    assert error.value.code is ValidationCode.MALFORMED_RESPONSE
