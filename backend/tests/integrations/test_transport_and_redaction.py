"""Transport contracts, redaction, and the single-repository boundary."""

from __future__ import annotations

import pytest
from conftest import TARGET_SHA

from app.integrations import (
    SUPERSET_REPOSITORY,
    ContractValidationError,
    HttpRequest,
    HttpResponse,
    RecordedTransport,
    RepositoryIdentity,
    StaticTokenProvider,
    TargetCommit,
    ValidationCode,
    parse_timestamp,
    redact_mapping,
    redact_text,
    require_field,
    require_json_array,
    require_json_object,
    require_superset_repository,
    validate_branch_name,
)
from app.integrations.json_values import JsonValue


def test_requests_must_use_https() -> None:
    with pytest.raises(ContractValidationError):
        HttpRequest(method="GET", url="http://api.github.com/repos")


def test_unsupported_http_methods_are_rejected() -> None:
    with pytest.raises(ContractValidationError):
        HttpRequest(method="TRACE", url="https://api.github.com/repos")


def test_recorded_transport_replays_scripted_responses_in_order() -> None:
    transport = RecordedTransport([HttpResponse(200, {"a": 1})])

    response = transport.send(
        HttpRequest(method="GET", url="https://api.github.com/repos")
    )

    assert response.json_body == {"a": 1}
    with pytest.raises(ContractValidationError) as error:
        transport.send(HttpRequest(method="GET", url="https://api.github.com/repos"))
    assert error.value.code is ValidationCode.TRANSPORT_FAILURE


def test_request_audit_view_redacts_credentials() -> None:
    request = HttpRequest(
        method="POST",
        url="https://api.github.com/repos?access_token=supersecretvalue",
        headers={"Authorization": "Bearer supersecretvalue"},
        json_body={"webhook_secret": "supersecretvalue", "body": "safe"},
    )

    rendered = str(request.redacted)

    assert "supersecretvalue" not in rendered
    assert "safe" in rendered


def test_json_object_and_array_helpers_reject_the_wrong_shape() -> None:
    with pytest.raises(ContractValidationError):
        require_json_object(HttpResponse(200, []), action="read")
    with pytest.raises(ContractValidationError):
        require_json_array(HttpResponse(200, {}), action="read")


def test_require_field_rejects_a_boolean_where_an_integer_is_expected() -> None:
    with pytest.raises(ContractValidationError):
        require_field({"number": True}, "number", int, action="read")
    assert require_field({"number": 4}, "number", int, action="read") == 4


def test_require_field_accepts_a_tuple_of_types() -> None:
    assert require_field({"c": 1}, "c", (int, float), action="read") == 1


def test_timestamps_must_be_timezone_aware_iso_8601() -> None:
    assert parse_timestamp("2026-01-01T12:00:00Z", "created_at").tzinfo is not None
    with pytest.raises(ContractValidationError):
        parse_timestamp("2026-01-01T12:00:00", "created_at")
    with pytest.raises(ContractValidationError):
        parse_timestamp("not a date", "created_at")
    with pytest.raises(ContractValidationError):
        parse_timestamp(None, "created_at")


def test_token_provider_never_renders_its_token() -> None:
    provider = StaticTokenProvider("supersecretvalue")

    assert "supersecretvalue" not in repr(provider)
    assert provider.token() == "supersecretvalue"


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdef1234567890",
        "https://example.com/x?api_key=abcdef1234567890",
        "ghp_0123456789abcdefghij",
    ],
)
def test_secret_shaped_text_is_redacted(text: str) -> None:
    redacted = redact_text(text)

    assert "abcdef1234567890" not in redacted
    assert "ghp_0123456789abcdefghij" not in redacted


def test_redaction_traverses_nested_structures() -> None:
    payload: dict[str, JsonValue] = {
        "issue": {"body": "token: ghp_0123456789abcdefghij"},
        "headers": [{"x-hub-signature-256": "sha256=deadbeef"}],
        "password": "hunter2",
        "safe": "chart export fails",
    }

    redacted = redact_mapping(payload)

    rendered = str(redacted)
    assert "ghp_0123456789abcdefghij" not in rendered
    assert "hunter2" not in rendered
    assert "deadbeef" not in rendered
    assert "chart export fails" in rendered


def test_only_the_superset_repository_is_authorized() -> None:
    assert require_superset_repository("exloong/superset") == SUPERSET_REPOSITORY
    assert require_superset_repository("Exloong/Superset") == SUPERSET_REPOSITORY

    for other in ("apache/superset", "exloong/superset-fork", "exloong/other"):
        with pytest.raises(ContractValidationError) as error:
            require_superset_repository(other)
        assert error.value.code is ValidationCode.UNAUTHORIZED_REPOSITORY


def test_repository_identity_requires_owner_and_name() -> None:
    with pytest.raises(ContractValidationError):
        RepositoryIdentity.from_full_name("exloong")
    with pytest.raises(ContractValidationError):
        require_superset_repository(None)


def test_target_commits_must_be_full_lowercase_shas() -> None:
    assert TargetCommit(sha=TARGET_SHA.upper()).sha == TARGET_SHA
    assert TargetCommit(sha=TARGET_SHA).short_sha == TARGET_SHA[:12]
    with pytest.raises(ContractValidationError):
        TargetCommit(sha="zz" + "a" * 38)


def test_branch_names_are_validated_against_git_ref_rules() -> None:
    assert validate_branch_name("relay/fix-40") == "relay/fix-40"
    for invalid in ("", "../etc", "relay//fix", "relay/fix/", "@{now}"):
        with pytest.raises(ContractValidationError):
            validate_branch_name(invalid)
